import os
import sys
import json
import time
import urllib.error
import urllib.request
import urllib.parse
import http.cookiejar
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from collections import defaultdict

# Set UTF-8 encoding
sys.stdout.reconfigure(encoding='utf-8')

# Environment variables (GitHub Actions secrets; no credential defaults)
POS_USER = os.environ.get('POS_USER')
POS_PASS = os.environ.get('POS_PASS')
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASS = os.environ.get('SMTP_PASS')
RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL', SMTP_USER)

# 3 attempts. Waits of 2 min + 8 min sit between them, about 10 minutes total.
POS_RETRY_ATTEMPTS = 3
POS_RETRY_WAITS_SECONDS = (120, 480)
POS_REQUEST_TIMEOUT = 60

class PosError(Exception):
    def __init__(self, error_type):
        self.error_type = error_type
        super().__init__(error_type)

def now_kst_now():
    return datetime.utcnow() + timedelta(hours=9)

def _header_value(headers, name):
    if headers is None:
        return ''
    getter = getattr(headers, 'get', None)
    if getter is None:
        return ''
    value = getter(name)
    if value:
        return value
    return getter(name.lower()) or ''

def _looks_like_html(content_type, body_text):
    ctype = (content_type or '').lower()
    sample = body_text.lstrip()[:40].lower()
    return 'text/html' in ctype or sample.startswith('<!doctype') or sample.startswith('<html')

def _http_error_type(code, body):
    if code == 530:
        snippet = body[:1000].decode('utf-8', errors='replace')
        if '1033' in snippet:
            return 'POS server 530 (Cloudflare 1033)'
        return 'POS server 530'
    return f'POS server {code}'

def _connection_error_type(exc):
    reason = getattr(exc, 'reason', exc)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return 'POS connection timeout'
    text = str(reason).lower()
    if 'timed out' in text or 'timeout' in text:
        return 'POS connection timeout'
    return 'POS connection error'

def _nonempty_cookie(cookie_jar):
    for cookie in cookie_jar:
        if (cookie.value or '').strip():
            return True
    return False

def _set_cookie_has_value(headers):
    lines = []
    if headers is not None and hasattr(headers, 'get_all'):
        lines = headers.get_all('Set-Cookie') or []
    else:
        raw = _header_value(headers, 'Set-Cookie')
        if raw:
            lines = [raw]
    for line in lines:
        pair = line.split(';', 1)[0]
        if '=' not in pair:
            continue
        _name, value = pair.split('=', 1)
        if value.strip():
            return True
    return False

def _read_response(opener, request, purpose, sleep):
    last_type = 'POS connection error'
    for attempt in range(1, POS_RETRY_ATTEMPTS + 1):
        try:
            with opener.open(request, timeout=POS_REQUEST_TIMEOUT) as resp:
                status = resp.getcode() if hasattr(resp, 'getcode') else resp.status
                return status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read() if exc.fp is not None else b''
            except OSError:
                body = b''
            if exc.code >= 500:
                last_type = _http_error_type(exc.code, body)
                print(f"[WARN] {purpose} failed ({last_type}), attempt {attempt}/{POS_RETRY_ATTEMPTS}")
            else:
                return exc.code, exc.headers, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_type = _connection_error_type(exc)
            print(f"[WARN] {purpose} failed ({last_type}), attempt {attempt}/{POS_RETRY_ATTEMPTS}")
        if attempt < POS_RETRY_ATTEMPTS:
            sleep(POS_RETRY_WAITS_SECONDS[attempt - 1])
    raise PosError(last_type)

def _assert_login(_status, headers, body, cookie_jar):
    text = body.decode('utf-8', errors='replace')
    if _looks_like_html(_header_value(headers, 'Content-Type'), text):
        raise PosError('POS login failed: login page')
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise PosError('POS login failed: login page')
    if not isinstance(data, dict) or not data.get('ok'):
        raise PosError('POS login failed')
    for key in ('token', 'access_token', 'session'):
        if key not in data or data.get(key) is None:
            continue
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PosError('POS login failed: empty token')
        return
    if not _nonempty_cookie(cookie_jar) and not _set_cookie_has_value(headers):
        raise PosError('POS login failed: empty token')

def _parse_orders(status, headers, body):
    text = body.decode('utf-8', errors='replace')
    if status >= 400 or _looks_like_html(_header_value(headers, 'Content-Type'), text):
        if _looks_like_html(_header_value(headers, 'Content-Type'), text):
            raise PosError('POS login failed: login page')
        raise PosError(f'POS data fetch failed (HTTP {status})')
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise PosError('POS data fetch failed')
    if not isinstance(data, dict) or 'orders' not in data:
        raise PosError('POS data fetch failed')
    orders = data['orders']
    if not isinstance(orders, list):
        raise PosError('POS data fetch failed')
    return orders

def fetch_pos_data(opener=None, sleep=time.sleep, cookie_jar=None):
    if not (POS_USER or '').strip() or not (POS_PASS or '').strip():
        raise PosError('POS_USER or POS_PASS environment variables are not set. Cannot log in to POS.')

    jar = cookie_jar if cookie_jar is not None else http.cookiejar.CookieJar()
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login_url = 'https://pos.blackupcoffeewerk.com/login'
    payload = json.dumps({'username': POS_USER, 'password': POS_PASS, 'remember': True}).encode('utf-8')
    login_req = urllib.request.Request(
        login_url,
        data=payload,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Content-Type': 'application/json'},
        method='POST'
    )
    status, headers, body = _read_response(opener, login_req, 'POS login', sleep)
    _assert_login(status, headers, body, jar)

    # 최근 7일치 넓게 조회 후 조건 필터링
    list_url = 'https://pos.blackupcoffeewerk.com/api/orders/list'
    now_kst = now_kst_now()
    today_str = now_kst.strftime('%Y-%m-%d')
    fetch_start = (now_kst - timedelta(days=7)).strftime('%Y-%m-%d')
    list_payload = json.dumps({'channel': '', 'ship_status': '', 'date_from': fetch_start, 'date_to': today_str, 'q': ''}).encode('utf-8')
    list_req = urllib.request.Request(
        list_url,
        data=list_payload,
        headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'},
        method='POST'
    )
    status, headers, body = _read_response(opener, list_req, 'POS order fetch', sleep)
    return _parse_orders(status, headers, body), now_kst

# Daily mail is due at 21:00 KST. A run that starts in the five hours before
# that waits. A run that starts earlier is the previous date's late delivery.
SEND_AT_HOUR = 21
LEAD_HOURS = 5
SENT_MARKER = os.path.join('.report-sent', 'date.txt')

def report_plan(now_kst):
    send_at = now_kst.replace(hour=SEND_AT_HOUR, minute=0, second=0, microsecond=0)
    window_open = send_at - timedelta(hours=LEAD_HOURS)
    if now_kst >= window_open:
        business_date = now_kst.date()
        wait_seconds = max(0, int((send_at - now_kst).total_seconds()))
    else:
        business_date = now_kst.date() - timedelta(days=1)
        wait_seconds = 0
    return business_date, wait_seconds

def resolve_plan(now_kst, mode, report_date_text):
    text = (report_date_text or '').strip()
    if text:
        business_date = datetime.strptime(text, '%Y-%m-%d').date()
        return business_date, 0
    business_date, wait_seconds = report_plan(now_kst)
    if mode != 'schedule':
        if now_kst.hour >= 9:
            business_date = now_kst.date()
        return business_date, 0
    return business_date, wait_seconds

def sales_window(business_date, now_kst):
    start = f"{business_date.isoformat()} 09:00"
    end = f"{(business_date + timedelta(days=1)).isoformat()} 00:00"
    if now_kst.date() == business_date:
        end_label = now_kst.strftime('%H:%M')
        if end_label < '09:00':
            end_label = '09:00'
    else:
        end_label = '24:00'
    period_label = f"{business_date.isoformat()} 당일 집계 (09:00~{end_label})"
    return start, end, period_label

def _order_stamp(order):
    return order.get('ordered_at', order.get('created_at', '')) or ''

def current_mode():
    mode = (os.environ.get('REPORT_MODE') or '').strip().lower()
    if mode in ('schedule', 'manual'):
        return mode
    if os.environ.get('GITHUB_EVENT_NAME') == 'schedule':
        return 'schedule'
    return 'manual'

def artifact_name(business_date):
    return f"pos-report-sent-{business_date.isoformat()}"

def already_sent(business_date):
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    repo = os.environ.get('GITHUB_REPOSITORY')
    if not token or not repo:
        return False
    url = (
        f"https://api.github.com/repos/{repo}/actions/artifacts"
        f"?name={artifact_name(business_date)}&per_page=1"
    )
    req = urllib.request.Request(
        url,
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2026-03-10',
            'User-Agent': 'blackup-pos-notifier',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] Could not check the sent marker ({type(exc).__name__}).")
        return False
    return int(data.get('total_count') or 0) > 0

def write_sent_marker(business_date):
    os.makedirs(os.path.dirname(SENT_MARKER), exist_ok=True)
    with open(SENT_MARKER, 'w', encoding='utf-8') as handle:
        handle.write(business_date.isoformat() + '\n')

def should_record(now_kst, business_date, mode):
    if mode == 'schedule':
        return True
    send_at = datetime.combine(business_date, datetime.min.time()).replace(hour=SEND_AT_HOUR)
    return now_kst >= send_at

def generate_report(orders, now_kst, business_date=None):
    if business_date is None:
        business_date, _wait = report_plan(now_kst)
    start, end, period_label = sales_window(business_date, now_kst)

    filtered = [o for o in orders if start <= _order_stamp(o) < end]

    prod_stats = defaultdict(lambda: {'orders': 0, 'qty': 0, 'amount': 0, 'canc_qty': 0})

    total_valid_orders = 0
    total_valid_qty = 0
    total_valid_amt = 0

    for o in filtered:
        p_name = o.get('product', '미지정 상품')
        q = int(o.get('qty', 1))
        amt = int(o.get('amount', 0))
        is_canc = o.get('ship_status') == '취소' or o.get('platform_status') == '취소' or o.get('pay_status') == '취소'
        
        if is_canc:
            prod_stats[p_name]['canc_qty'] += q
        else:
            prod_stats[p_name]['orders'] += 1
            prod_stats[p_name]['qty'] += q
            prod_stats[p_name]['amount'] += amt
            total_valid_orders += 1
            total_valid_qty += q
            total_valid_amt += amt

    sorted_prods = sorted(prod_stats.items(), key=lambda x: x[1]['qty'], reverse=True)
    report_date_str = now_kst.strftime('%Y년 %m월 %d일 %H:%M')
    if filtered:
        warning_html = ''
        subject = f"☕ [블랙업 POS] {period_label} 리포트"
    else:
        warning_html = '<div class="warn">경고: 집계 구간에서 주문이 0건입니다. POS 데이터를 확인하세요.</div>'
        subject = f"⚠ [블랙업 POS] 주문 0건 — POS 데이터를 확인하세요 | {period_label}"

    # HTML Email Body
    html_body = f"""
    <html>
    <head>
        <style>
            body {{ font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; color: #222; background-color: #f8f9fa; padding: 20px; }}
            .container {{ max-width: 680px; margin: 0 auto; background: #ffffff; border-radius: 12px; padding: 28px; box-shadow: 0 4px 15px rgba(0,0,0,0.06); border: 1px solid #e1e4e8; }}
            h2 {{ color: #1a1a1a; font-size: 22px; border-bottom: 2px solid #1a1a1a; padding-bottom: 10px; margin-top: 0; }}
            .badge {{ background-color: #e0f2fe; color: #0369a1; padding: 4px 10px; border-radius: 6px; font-size: 13px; font-weight: bold; display: inline-block; margin-bottom: 10px; }}
            .kpi-box {{ display: flex; gap: 12px; margin: 20px 0; }}
            .kpi-card {{ flex: 1; background: #f1f5f9; padding: 16px; border-radius: 8px; text-align: center; }}
            .kpi-title {{ font-size: 13px; color: #64748b; margin-bottom: 4px; }}
            .kpi-value {{ font-size: 20px; font-weight: bold; color: #0f172a; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 14px; }}
            th {{ background-color: #1e293b; color: #ffffff; padding: 10px; text-align: left; }}
            td {{ padding: 10px; border-bottom: 1px solid #e2e8f0; }}
            tr:nth-child(even) {{ background-color: #f8fafc; }}
            .highlight {{ color: #2563eb; font-weight: bold; }}
            .warn {{ background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; padding: 12px 14px; border-radius: 8px; font-weight: bold; margin: 0 0 16px 0; }}
            .footer {{ margin-top: 30px; font-size: 12px; color: #94a3b8; text-align: center; border-top: 1px solid #e2e8f0; padding-top: 15px; }}
        </style>
    </head>
    <body>
        <div class="container">
            {warning_html}
            <h2>☕ 블랙업커피 POS 맞춤 판매집계 리포트</h2>
            <p style="color:#64748b; font-size:13px; margin-bottom: 6px;">집계 일시: {report_date_str} KST</p>
            <div><span class="badge">📅 {period_label}</span></div>
            
            <div class="kpi-box">
                <div class="kpi-card">
                    <div class="kpi-title">총 유효 주문건수</div>
                    <div class="kpi-value">{total_valid_orders:,} 건</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">총 유효 주문수량</div>
                    <div class="kpi-value">{total_valid_qty:,} 개</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">총 유효 매출액</div>
                    <div class="kpi-value">{total_valid_amt:,} 원</div>
                </div>
            </div>

            <h3>📊 품목별 주문 수량 순위 ({period_label})</h3>
            <table>
                <thead>
                    <tr>
                        <th>순위</th>
                        <th>상품명</th>
                        <th>주문건수</th>
                        <th>주문수량</th>
                        <th>매출액</th>
                    </tr>
                </thead>
                <tbody>
    """

    for rank, (p_name, st) in enumerate(sorted_prods, 1):
        o_cnt = st['orders']
        q_cnt = st['qty']
        a_cnt = st['amount']
        hl_class = 'class="highlight"' if '데일리팩' in p_name or '누룩' in p_name else ''
        html_body += f"""
                    <tr>
                        <td align="center">{rank}</td>
                        <td {hl_class}>{p_name}</td>
                        <td align="center">{o_cnt}건</td>
                        <td align="center"><b>{q_cnt}개</b></td>
                        <td align="right">{a_cnt:,}원</td>
                    </tr>
        """

    html_body += f"""
                </tbody>
            </table>

            <div class="footer">
                본 메일은 GitHub Actions 클라우드 스케줄러에 의해 자동으로 생성 및 발송되었습니다.<br>
                요일과 상관없이 해당 날짜 09:00~24:00 KST 매출만 집계합니다.
            </div>
        </div>
    </body>
    </html>
    """

    return html_body, subject

def _recipient():
    return RECIPIENT_EMAIL if RECIPIENT_EMAIL else SMTP_USER

def _deliver(subject, body, subtype):
    if not SMTP_USER or not SMTP_PASS:
        print("[ERROR] SMTP_USER or SMTP_PASS environment variables are not set. Cannot send email.")
        raise PosError('SMTP credentials missing')

    recip = _recipient()
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"블랙업 POS 자동 리포트 <{SMTP_USER}>"
    msg['To'] = recip

    part = MIMEText(body, subtype, 'utf-8')
    msg.attach(part)

    print("Connecting to Gmail SMTP server (smtp.gmail.com:587)...")
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, recip, msg.as_string())
    except PosError:
        raise
    except Exception as exc:
        print(f"[ERROR] Email send failed ({type(exc).__name__}).")
        raise PosError('email send failed')
    return recip

def send_email(subject, html_content):
    recip = _deliver(subject, html_content, 'html')
    print(f"[SUCCESS] Email report successfully sent to {recip}!")

def send_failure_email(error_type, when_kst):
    if not SMTP_USER or not SMTP_PASS:
        print("[ERROR] SMTP_USER or SMTP_PASS environment variables are not set. Cannot send email.")
        return
    when_text = when_kst.strftime('%Y-%m-%d %H:%M KST')
    subject = f"[블랙업 POS] 리포트 실패: {error_type}"
    body = (
        "블랙업 POS 자동 리포트가 실패했습니다.\n"
        f"오류 유형: {error_type}\n"
        f"실행 시각: {when_text}\n"
    )
    try:
        recip = _deliver(subject, body, 'plain')
    except PosError:
        print("[ERROR] Failure email could not be sent.")
        return
    print(f"[WARN] Failure email sent to {recip}.")

def _force_send():
    return (os.environ.get('FORCE_SEND') or '').strip().lower() in ('1', 'true', 'yes')

def run(now=None, sleep=time.sleep, already_sent_fn=None, mode=None, report_date_text=None, force=None):
    print("Starting Cloud Report Execution with Custom Business Logic...")
    now_kst = now if now is not None else now_kst_now()
    if not (POS_USER or '').strip() or not (POS_PASS or '').strip():
        error_type = 'POS_USER or POS_PASS environment variables are not set. Cannot log in to POS.'
        print(f"[ERROR] {error_type}")
        send_failure_email(error_type, now_kst)
        return 1
    mode = mode if mode is not None else current_mode()
    if report_date_text is None:
        report_date_text = os.environ.get('REPORT_DATE', '')
    if force is None:
        force = _force_send()
    try:
        business_date, wait_seconds = resolve_plan(now_kst, mode, report_date_text)
    except ValueError:
        print("[ERROR] REPORT_DATE must be YYYY-MM-DD.")
        return 1
    checker = already_sent_fn if already_sent_fn is not None else already_sent
    if not force and checker(business_date):
        print(f"[SKIP] Report for {business_date.isoformat()} was already sent.")
        return 0
    if wait_seconds > 0:
        print(f"[WAIT] Report date {business_date.isoformat()}. Sleeping {wait_seconds} seconds until 21:00 KST.")
        sleep(wait_seconds)
        now_kst = now_kst + timedelta(seconds=wait_seconds)
        if not force and checker(business_date):
            print(f"[SKIP] Report for {business_date.isoformat()} was already sent.")
            return 0
    try:
        orders, fetched_at = fetch_pos_data()
        label_now = now_kst if now is not None else fetched_at
        print(f"Fetched {len(orders)} total orders from POS. Report date {business_date.isoformat()}.")
        html_report, subject = generate_report(orders, label_now, business_date=business_date)
        send_email(subject, html_report)
        if should_record(label_now, business_date, mode):
            write_sent_marker(business_date)
        return 0
    except PosError as exc:
        print(f"[ERROR] {exc.error_type}")
        send_failure_email(exc.error_type, now_kst_now())
        return 1
    except Exception:
        print("[ERROR] POS report error")
        send_failure_email('POS report error', now_kst_now())
        return 1

if __name__ == '__main__':
    sys.exit(run())
