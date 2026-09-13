# 📘 초보자를 위한 클라우드 자동화 세팅 및 동작 리포트

사용자님의 PC가 완전히 꺼져 있어도 **매일 오후 9시(21:00 KST)**에 자동으로 POS 데이터를 집계하여 정갈한 HTML 이메일 리포트를 보내주는 시스템 구축 결과 및 가이드입니다.

---

## 💡 1. 컴퓨터가 꺼져도 작동하는 원리 (개념 이해)

1. **로컬 PC vs 클라우드 서버**:
   - 사용자님의 컴퓨터는 밤에 꺼질 수 있지만, **GitHub(마이크로소프트의 글로벌 개발 플랫폼)**의 가상 서버는 365일 24시간 켜져 있습니다.
2. **GitHub Actions 스케줄러**:
   - 저희가 만든 파이썬 수집 코드와 설정 파일(`.github/workflows/daily_report.yml`)을 GitHub에 올려두면, **한국시간 매일 오후 9시 00분**에 GitHub 가상 컴퓨터가 알아서 깨어나 POS 데이터를 조회하고 이메일을 발송합니다.
3. **보안 (GitHub Secrets)**:
   - POS 아이디/비밀번호, 메일 접속 비밀번호는 소스코드에 드러나지 않으며, GitHub의 암호화 저장소(Secrets)에 안전하게 보관됩니다.

---

## 🛠️ 2. AI가 자동으로 완성해 둔 항목 (자동 진행 리포트)

- ✅ **`pos_report.py`**: POS 자동 로그인, 실시간 주문 데이터 수집, 취소건 분리, 9/11 이후 수량 집계, 모바일/PC 겸용 HTML 프리미엄 이메일 양식 자동 생성 코드 작성 완료
- ✅ **`.github/workflows/daily_report.yml`**: 한국시간 오후 9시(`0 12 * * *` UTC) 정기 실행 및 수동 실행(Run workflow) 기능 탑재 완료
- ✅ **`requirements.txt`**: 파이썬 클라우드 실행 환경 설정 완료

---

## 🚀 3. 사용자님이 꼭 입력하셔야 하는 3단계 (최종 2분 완성)

아래 3단계만 진행하시면 오늘 밤 9시부터 PC를 끄셔도 메일이 자동으로 도착합니다!

### [1단계] Gmail 16자리 앱 비밀번호 생성 (1분)
1. Google 계정 설정([myaccount.google.com](https://myaccount.google.com)) 접속
2. **보안** 탭 -> **2단계 인증** 활성화 (이미 되어있다면 통과)
3. 제일 아래 **앱 비밀번호** 검색 또는 접속
4. 앱 이름 입력(예: `POS알림`) 후 **생성** 버튼 클릭
5. 화면에 나오는 **16자리 비밀번호**(예: `abcd efgh ijkl mnop`) 복사해두기

### [2단계] GitHub 저장소(Repository) 만들기 (1분)
1. [github.com](https://github.com) 접속 및 로그인 (계정이 없다면 1분 가입)
2. 우측 상단 **`+`** 버튼 -> **New repository** 클릭
3. Repository name: `blackup-pos-notifier` 입력 후 **Create repository** 클릭
4. 프로젝트 폴더(`C:\Users\jacob\.gemini\antigravity\scratch\blackup-pos-cloud-notifier`)의 파일들을 해당 저장소에 업로드합니다.

### [3단계] GitHub Secrets (비밀번호 저장소) 등록 (30초)
새로 만든 GitHub 저장소 페이지에서:
1. **Settings** 탭 -> 좌측 **Secrets and variables** -> **Actions** 클릭
2. **New repository secret** 버튼 클릭 후 아래 5개 항목 입력:

| Secret 이름 | 입력할 값 | 설명 |
| :--- | :--- | :--- |
| `POS_USER` | `jacobriri` | POS 아이디 |
| `POS_PASS` | `0406` | POS 비밀번호 |
| `SMTP_USER` | `사용자님의Gmail@gmail.com` | 발신용 Gmail 주소 |
| `SMTP_PASS` | `1단계에서 만든 16자리 앱비밀번호` | Gmail 앱 비밀번호 |
| `RECIPIENT_EMAIL` | `리포트를 받을 이메일 주소` | 수신 메일 주소 |

---

## 🧪 4. 클라우드 즉시 테스트 방법

GitHub 저장소의 **Actions** 탭 -> **Daily POS Sales Email Report** 클릭 -> **Run workflow** 버튼을 클릭하면 오후 9시까지 기다리지 않고 지금 즉시 클라우드가 발송하는 메일을 받아보실 수 있습니다!
