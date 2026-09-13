import os
import sys
import json
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

# Environment variables
POS_USER = os.environ.get('POS_USER', 'jacobriri')
POS_PASS = os.environ.get('POS_PASS', '0406')
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASS = os.environ.get('SMTP_PASS')
RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL', SMTP_USER)

# 수동 고정 날짜 (설정 안 되어있으면 자동 평일/주말 맞춤 계산)
CUTOFF_DATE = os.environ.get('CUTOFF_DATE', '')

def fetch_pos_data():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    # 1. Login
    login_url = 'https://pos.blackupcoffeewerk.com/login'
    payload = json.dumps({'username': POS_USER, 'password': POS_PASS, 'remember': True}).encode('utf-8')
    req = urllib.request.Request(
        login_url,
        data=payload,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Content-Type': 'application/json'},
        method='POST'
    )
    opener.open(req)

    # 2. Fetch orders (최근 7일치 넓게 조회 후 조건 필터링)
    list_url = 'https://pos.blackupcoffeewerk.com/api/orders/list'
    now_kst = datetime.utcnow() + timedelta(hours=9)
    today_str = now_kst.strftime('%Y-%m-%d')
    fetch_start = (now_kst - timedelta(days=7)).strftime('%Y-%m-%d')
    
    p = json.dumps({'channel': '', 'ship_status': '', 'date_from': fetch_start, 'date_to': today_str, 'q': ''}).encode('utf-8')
    r = urllib.request.Request(
        list_url,
        data=p,
        headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'},
        method='POST'
    )
    
    with opener.open(r) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        return data.get('orders', []), now_kst

def generate_report(orders, now_kst):
    # 요일 판단: 0=월, 1=화, 2=수, 3=목, 4=금, 5=토, 6=일
    weekday = now_kst.weekday()

    if CUTOFF_DATE:
        target_cutoff = CUTOFF_DATE
        period_label = f"{target_cutoff} 이후 집계 (수동 지정)"
    elif weekday in [5, 6]:  # 주말 (토요일, 일요일)
        days_back = 1 if weekday == 5 else 2
        fri_date = (now_kst - timedelta(days=days_back)).strftime('%Y-%m-%d')
        target_cutoff = f"{fri_date} 09:00"
        day_name = "토요일" if weekday == 5 else "일요일"
        period_label = f"주말 누적 집계 (금요일 {fri_date} 09:00 ~ {day_name} 현재)"
    else:  # 평일 (월~금)
        today_str = now_kst.strftime('%Y-%m-%d')
        target_cutoff = f"{today_str} 09:00"
        period_label = f"평일 당일 집계 ({today_str} 09:00 ~ 현재)"

    filtered = [o for o in orders if o.get('ordered_at', o.get('created_at', '')) >= target_cutoff]

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
            .footer {{ margin-top: 30px; font-size: 12px; color: #94a3b8; text-align: center; border-top: 1px solid #e2e8f0; padding-top: 15px; }}
        </style>
    </head>
    <body>
        <div class="container">
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
                평일: 당일 오전 09:00 이후 집계 | 주말: 금요일 오전 09:00 이후 누적 집계
            </div>
        </div>
    </body>
    </html>
    """

    return html_body, f"☕ [블랙업 POS] {period_label} 리포트"

def send_email(subject, html_content):
    if not SMTP_USER or not SMTP_PASS:
        print("[ERROR] SMTP_USER or SMTP_PASS environment variables are not set. Cannot send email.")
        sys.exit(1)

    recip = RECIPIENT_EMAIL if RECIPIENT_EMAIL else SMTP_USER

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"블랙업 POS 자동 리포트 <{SMTP_USER}>"
    msg['To'] = recip

    part = MIMEText(html_content, 'html', 'utf-8')
    msg.attach(part)

    print(f"Connecting to Gmail SMTP server (smtp.gmail.com:587)...")
    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, recip, msg.as_string())
    print(f"[SUCCESS] Email report successfully sent to {recip}!")

if __name__ == '__main__':
    print("Starting Cloud Report Execution with Custom Business Logic...")
    orders, now_kst = fetch_pos_data()
    print(f"Fetched {len(orders)} total orders from POS.")
    html_report, subject = generate_report(orders, now_kst)
    send_email(subject, html_report)
