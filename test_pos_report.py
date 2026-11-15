import email
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta
from email.header import decode_header
from email.message import Message
from http.cookiejar import Cookie, CookieJar

import pos_report

POS_PASSWORD = 'test-pass-not-real'
SMTP_PASSWORD = 'test-smtp-not-real'
LOGIN_URL = 'https://pos.blackupcoffeewerk.com/login'
LIST_URL = 'https://pos.blackupcoffeewerk.com/api/orders/list'


def message_headers(content_type='application/json', set_cookie=''):
    headers = Message()
    headers['Content-Type'] = content_type
    if set_cookie:
        headers['Set-Cookie'] = set_cookie
    return headers


def http_error(url, code, body, content_type='text/html'):
    payload = body if isinstance(body, bytes) else body.encode('utf-8')
    return urllib.error.HTTPError(url, code, 'error', message_headers(content_type), io.BytesIO(payload))


class FakeResponse:
    def __init__(self, status, body, headers):
        self.status = status
        self.headers = headers
        self._body = body if isinstance(body, bytes) else body.encode('utf-8')

    def getcode(self):
        return self.status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ScriptedOpener:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, timeout))
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


def login_ok(token='', cookie=''):
    payload = {'ok': True}
    if token:
        payload['token'] = token
    set_cookie = f'session={cookie}; Path=/; HttpOnly' if cookie else ''
    return FakeResponse(200, json.dumps(payload), message_headers(set_cookie=set_cookie))


def orders_ok(orders):
    return FakeResponse(200, json.dumps({'orders': orders}), message_headers())


def remember_cookie(jar, value):
    jar.set_cookie(Cookie(
        version=0, name='session', value=value, port=None, port_specified=False,
        domain='pos.blackupcoffeewerk.com', domain_specified=True, domain_initial_dot=False,
        path='/', path_specified=True, secure=True, expires=None, discard=True,
        comment=None, comment_url=None, rest={},
    ))


class CaptureSMTP:
    instances = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.messages = []
        CaptureSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        return None

    def login(self, user, password):
        self.user = user
        self.password = password

    def sendmail(self, sender, recip, message):
        self.messages.append((sender, recip, message))


def decoded_mail(raw):
    msg = email.message_from_string(raw)
    subject_parts = []
    for text, charset in decode_header(msg['Subject']):
        if isinstance(text, bytes):
            subject_parts.append(text.decode(charset or 'utf-8'))
        else:
            subject_parts.append(text)
    bodies = []
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        bodies.append(payload.decode(part.get_content_charset() or 'utf-8'))
    return ''.join(subject_parts), '\n'.join(bodies)


class ReportBehaviorTest(unittest.TestCase):
    def setUp(self):
        self._pos_user = pos_report.POS_USER
        self._pos_pass = pos_report.POS_PASS
        self._smtp_user = pos_report.SMTP_USER
        self._smtp_pass = pos_report.SMTP_PASS
        self._recipient = pos_report.RECIPIENT_EMAIL
        self._fetch = pos_report.fetch_pos_data
        self._smtp = pos_report.smtplib.SMTP
        self._now = pos_report.now_kst_now
        pos_report.POS_USER = 'report-user'
        pos_report.POS_PASS = POS_PASSWORD
        pos_report.SMTP_USER = 'sender@example.com'
        pos_report.SMTP_PASS = SMTP_PASSWORD
        pos_report.RECIPIENT_EMAIL = 'owner@example.com'
        CaptureSMTP.instances = []
        pos_report.smtplib.SMTP = CaptureSMTP

    def tearDown(self):
        pos_report.POS_USER = self._pos_user
        pos_report.POS_PASS = self._pos_pass
        pos_report.SMTP_USER = self._smtp_user
        pos_report.SMTP_PASS = self._smtp_pass
        pos_report.RECIPIENT_EMAIL = self._recipient
        pos_report.fetch_pos_data = self._fetch
        pos_report.smtplib.SMTP = self._smtp
        pos_report.now_kst_now = self._now
        marker = os.path.join('.report-sent', 'date.txt')
        if os.path.exists(marker):
            os.remove(marker)

    def test_retries_530_then_succeeds_with_ten_minute_backoff(self):
        cloudflare = http_error(LOGIN_URL, 530, '<html>error code: 1033</html>')
        opener = ScriptedOpener([cloudflare, cloudflare, login_ok(cookie='abc'), orders_ok([])])
        sleeps = []
        orders, _now = pos_report.fetch_pos_data(opener=opener, sleep=sleeps.append, cookie_jar=CookieJar())
        self.assertEqual(orders, [])
        self.assertEqual(sleeps, [120, 480])
        self.assertEqual(sum(sleeps), 600)
        self.assertEqual(len(opener.calls), 4)
        self.assertEqual(opener.calls[0][1], 60)

    def test_single_connection_error_waits_once(self):
        opener = ScriptedOpener([
            urllib.error.URLError(TimeoutError('timed out')),
            login_ok(token='session-token'),
            orders_ok([{'ordered_at': '2026-11-12 10:00'}]),
        ])
        sleeps = []
        orders, _now = pos_report.fetch_pos_data(opener=opener, sleep=sleeps.append, cookie_jar=CookieJar())
        self.assertEqual(len(orders), 1)
        self.assertEqual(sleeps, [120])

    def test_three_530s_raise_without_logging_secrets(self):
        opener = ScriptedOpener([
            http_error(LOGIN_URL, 530, 'cloudflare'),
            http_error(LOGIN_URL, 530, 'cloudflare'),
            http_error(LOGIN_URL, 530, 'cloudflare'),
        ])
        sleeps = []
        stdout = io.StringIO()
        original = sys.stdout
        sys.stdout = stdout
        try:
            with self.assertRaises(pos_report.PosError) as caught:
                pos_report.fetch_pos_data(opener=opener, sleep=sleeps.append, cookie_jar=CookieJar())
        finally:
            sys.stdout = original
        self.assertEqual(caught.exception.error_type, 'POS server 530')
        self.assertEqual(sleeps, [120, 480])
        logged = stdout.getvalue()
        self.assertNotIn(POS_PASSWORD, logged)
        self.assertNotIn(SMTP_PASSWORD, logged)
        self.assertIn('attempt 3/3', logged)

    def test_login_page_and_empty_token_fail_without_retry(self):
        page = FakeResponse(200, '<!doctype html><title>로그인</title>', message_headers('text/html'))
        sleeps = []
        with self.assertRaises(pos_report.PosError) as caught:
            pos_report.fetch_pos_data(opener=ScriptedOpener([page]), sleep=sleeps.append, cookie_jar=CookieJar())
        self.assertEqual(caught.exception.error_type, 'POS login failed: login page')
        self.assertEqual(sleeps, [])

        empty = FakeResponse(200, json.dumps({'ok': True, 'token': ''}), message_headers())
        with self.assertRaises(pos_report.PosError) as caught:
            pos_report.fetch_pos_data(opener=ScriptedOpener([empty]), sleep=sleeps.append, cookie_jar=CookieJar())
        self.assertEqual(caught.exception.error_type, 'POS login failed: empty token')
        self.assertEqual(sleeps, [])

        denied = http_error(LOGIN_URL, 401, json.dumps({'ok': False}), 'application/json')
        with self.assertRaises(pos_report.PosError) as caught:
            pos_report.fetch_pos_data(opener=ScriptedOpener([denied]), sleep=sleeps.append, cookie_jar=CookieJar())
        self.assertEqual(caught.exception.error_type, 'POS login failed')
        self.assertEqual(sleeps, [])

    def test_cookie_jar_counts_as_session_token(self):
        jar = CookieJar()
        remember_cookie(jar, 'jar-session')
        body = FakeResponse(200, json.dumps({'ok': True}), message_headers())
        orders, _now = pos_report.fetch_pos_data(
            opener=ScriptedOpener([body, orders_ok([])]),
            sleep=lambda _seconds: None,
            cookie_jar=jar,
        )
        self.assertEqual(orders, [])

    def test_order_payload_without_orders_fails(self):
        unexpected = FakeResponse(200, json.dumps({'ok': False}), message_headers())
        with self.assertRaises(pos_report.PosError) as caught:
            pos_report.fetch_pos_data(
                opener=ScriptedOpener([login_ok(cookie='abc'), unexpected]),
                sleep=lambda _seconds: None,
                cookie_jar=CookieJar(),
            )
        self.assertEqual(caught.exception.error_type, 'POS data fetch failed')

    def test_order_fetch_502_retries_then_fails(self):
        opener = ScriptedOpener([
            login_ok(cookie='abc'),
            http_error(LIST_URL, 502, 'bad gateway'),
            http_error(LIST_URL, 502, 'bad gateway'),
            http_error(LIST_URL, 502, 'bad gateway'),
        ])
        sleeps = []
        with self.assertRaises(pos_report.PosError) as caught:
            pos_report.fetch_pos_data(opener=opener, sleep=sleeps.append, cookie_jar=CookieJar())
        self.assertEqual(caught.exception.error_type, 'POS server 502')
        self.assertEqual(sleeps, [120, 480])

    def test_zero_orders_in_period_warns_but_still_sends(self):
        when = datetime(2026, 11, 12, 21, 0)
        outside = {
            'ordered_at': '2026-11-11 12:00',
            'product': '원두',
            'qty': 2,
            'amount': 2000,
        }
        pos_report.fetch_pos_data = lambda: ([outside], when)
        pos_report.now_kst_now = lambda: when
        code = pos_report.run(sleep=lambda _seconds: None, already_sent_fn=lambda _date: False, mode='schedule')
        self.assertEqual(code, 0)
        self.assertEqual(len(CaptureSMTP.instances), 1)
        subject, body = decoded_mail(CaptureSMTP.instances[0].messages[0][2])
        self.assertIn('주문 0건', subject)
        self.assertIn('POS 데이터를 확인하세요', subject)
        self.assertIn('집계 구간에서 주문이 0건입니다', body)
        self.assertNotIn(POS_PASSWORD, subject + body)
        self.assertEqual(CaptureSMTP.instances[0].messages[0][1], 'owner@example.com')

    def test_in_period_order_keeps_normal_subject(self):
        when = datetime(2026, 11, 12, 21, 0)
        inside = {
            'ordered_at': '2026-11-12 10:00',
            'product': '원두',
            'qty': 1,
            'amount': 1000,
        }
        _html, subject = pos_report.generate_report([inside], when)
        self.assertEqual(subject, '☕ [블랙업 POS] 2026-11-12 당일 집계 (09:00~21:00) 리포트')
        self.assertNotIn('0건', subject)
        self.assertNotIn('주말 누적', subject)

    def test_late_saturday_run_keeps_friday_same_day_window(self):
        started = datetime(2026, 11, 14, 3, 48, 30)
        business_date, wait_seconds = pos_report.report_plan(started)
        self.assertEqual(business_date.isoformat(), '2026-11-13')
        self.assertEqual(wait_seconds, 0)
        orders = [
            {'ordered_at': '2026-11-13 08:30', 'product': '이른원두', 'qty': 1, 'amount': 1000},
            {'ordered_at': '2026-11-13 10:00', 'product': '원두', 'qty': 2, 'amount': 4000},
            {'ordered_at': '2026-11-13 22:40', 'product': '디카페인', 'qty': 1, 'amount': 3000},
            {'ordered_at': '2026-11-14 01:10', 'product': '새벽원두', 'qty': 9, 'amount': 9000},
        ]
        html, subject = pos_report.generate_report(orders, started, business_date=business_date)
        self.assertIn('2026-11-13 당일 집계 (09:00~24:00)', subject)
        self.assertNotIn('주말 누적', subject)
        self.assertIn('원두', html)
        self.assertIn('디카페인', html)
        self.assertNotIn('이른원두', html)
        self.assertNotIn('새벽원두', html)
        self.assertNotIn('9개', html)

    def test_schedule_waits_until_2100_and_skips_a_duplicate(self):
        waits = []
        fetched = []

        def fetch():
            fetched.append(True)
            return ([], datetime(2026, 11, 13, 21, 0))

        pos_report.fetch_pos_data = fetch
        code = pos_report.run(
            now=datetime(2026, 11, 13, 20, 0),
            sleep=waits.append,
            already_sent_fn=lambda _date: False,
            mode='schedule',
        )
        self.assertEqual(code, 0)
        self.assertEqual(waits, [3600])
        self.assertEqual(fetched, [True])
        subject, _body = decoded_mail(CaptureSMTP.instances[0].messages[0][2])
        self.assertIn('2026-11-13 당일 집계 (09:00~21:00)', subject)

        def fail_if_fetched():
            fetched.append('again')
            raise AssertionError('duplicate run fetched POS data')

        pos_report.fetch_pos_data = fail_if_fetched
        duplicate = pos_report.run(
            now=datetime(2026, 11, 14, 3, 48),
            sleep=waits.append,
            already_sent_fn=lambda date: date.isoformat() == '2026-11-13',
            mode='schedule',
        )
        self.assertEqual(duplicate, 0)
        self.assertEqual(fetched, [True])
        self.assertEqual(len(CaptureSMTP.instances[0].messages), 1)

    def test_window_edges_around_1600_and_2100(self):
        business_date, wait_seconds = pos_report.report_plan(datetime(2026, 11, 14, 14, 15))
        self.assertEqual(business_date.isoformat(), '2026-11-13')
        self.assertEqual(wait_seconds, 0)
        business_date, wait_seconds = pos_report.report_plan(datetime(2026, 11, 14, 17, 0))
        self.assertEqual(business_date.isoformat(), '2026-11-14')
        self.assertEqual(wait_seconds, 4 * 3600)
        business_date, wait_seconds = pos_report.report_plan(datetime(2026, 11, 13, 21, 30))
        self.assertEqual(business_date.isoformat(), '2026-11-13')
        self.assertEqual(wait_seconds, 0)

    def test_observed_schedule_delays_still_target_the_same_kst_date(self):
        # Slots are UTC hour:15. Delays are the min, median, and max measured
        # from the 30 schedule runs between 2026-10-15 and 2026-11-13.
        slots = (2, 5, 8, 11)
        for delay_hours in (2.54, 5.35, 9.68):
            hits = []
            for hour in slots:
                start_utc = datetime(2026, 11, 13, hour, 15) + timedelta(hours=delay_hours)
                start_kst = start_utc + timedelta(hours=9)
                business_date, wait_seconds = pos_report.report_plan(start_kst)
                if business_date.isoformat() == '2026-11-13' and wait_seconds <= 5 * 3600:
                    if start_kst.hour > 15 or (start_kst.hour == 16 and wait_seconds <= 5 * 3600):
                        hits.append(hour)
            self.assertTrue(hits, delay_hours)

    def test_failure_email_then_nonzero_exit(self):
        def fail_fetch():
            raise pos_report.PosError('POS server 530')

        pos_report.fetch_pos_data = fail_fetch
        pos_report.now_kst_now = lambda: datetime(2026, 11, 12, 21, 5)
        code = pos_report.run(sleep=lambda _seconds: None, already_sent_fn=lambda _date: False, mode='schedule')
        self.assertEqual(code, 1)
        subject, body = decoded_mail(CaptureSMTP.instances[0].messages[0][2])
        self.assertIn('리포트 실패', subject)
        self.assertIn('POS server 530', subject)
        self.assertIn('POS server 530', body)
        self.assertIn('2026-11-12 21:05 KST', body)
        self.assertIn('실패했습니다', body)
        raw = CaptureSMTP.instances[0].messages[0][2]
        self.assertNotIn(POS_PASSWORD, raw)
        self.assertNotIn(SMTP_PASSWORD, raw)
        self.assertEqual(CaptureSMTP.instances[0].messages[0][1], 'owner@example.com')

    def test_missing_credentials_exit_nonzero_without_network(self):
        env = {'PATH': os.environ.get('PATH', ''), 'PYTHONIOENCODING': 'utf-8'}
        proc = subprocess.run(
            [sys.executable, 'pos_report.py'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('POS_USER or POS_PASS environment variables are not set', proc.stdout)
        self.assertNotIn(POS_PASSWORD, proc.stdout)
        self.assertNotIn('smtp.gmail.com', proc.stdout)


if __name__ == '__main__':
    unittest.main()
