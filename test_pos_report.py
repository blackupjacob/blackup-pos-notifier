import email
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from datetime import datetime
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
        self._cutoff = pos_report.CUTOFF_DATE
        self._fetch = pos_report.fetch_pos_data
        self._smtp = pos_report.smtplib.SMTP
        self._now = pos_report.now_kst_now
        pos_report.POS_USER = 'report-user'
        pos_report.POS_PASS = POS_PASSWORD
        pos_report.SMTP_USER = 'sender@example.com'
        pos_report.SMTP_PASS = SMTP_PASSWORD
        pos_report.RECIPIENT_EMAIL = 'owner@example.com'
        pos_report.CUTOFF_DATE = ''
        CaptureSMTP.instances = []
        pos_report.smtplib.SMTP = CaptureSMTP

    def tearDown(self):
        pos_report.POS_USER = self._pos_user
        pos_report.POS_PASS = self._pos_pass
        pos_report.SMTP_USER = self._smtp_user
        pos_report.SMTP_PASS = self._smtp_pass
        pos_report.RECIPIENT_EMAIL = self._recipient
        pos_report.CUTOFF_DATE = self._cutoff
        pos_report.fetch_pos_data = self._fetch
        pos_report.smtplib.SMTP = self._smtp
        pos_report.now_kst_now = self._now

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
        code = pos_report.run()
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
        self.assertEqual(subject, '☕ [블랙업 POS] 평일 당일 집계 (2026-11-12 09:00 ~ 현재) 리포트')
        self.assertNotIn('0건', subject)

    def test_failure_email_then_nonzero_exit(self):
        def fail_fetch():
            raise pos_report.PosError('POS server 530')

        pos_report.fetch_pos_data = fail_fetch
        pos_report.now_kst_now = lambda: datetime(2026, 11, 12, 21, 5)
        code = pos_report.run()
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
