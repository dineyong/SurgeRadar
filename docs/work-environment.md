# SURGERADAR 기기별 개발 환경

SURGERADAR는 Alpaca 데이터 수신과 급등 탐지, 보통주 후보 필터를 검증하는 Python 프로젝트다. 맥과 Windows에서 같은 작업 브랜치와 fixture를 사용하고 실제 계정 설정은 각 기기에 따로 지정한다.

## 저장소와 작업 폴더

- GitHub: https://github.com/dineyong/SurgeRadar
- 회사 기준 폴더: `C:\Users\USER\Documents\New project\SurgeRadar`
- 2026년 10월 8일 확인한 브랜치: `feature/issue-7-common-stock-filter`, 커밋 `336cc9e`, 패키지 버전 0.5.0.
- `C:\Users\USER\Documents\SurgeRadar`는 이전 `feature/issue-6-universe-selector`의 `f2dee1f` 복사본이다. 이번 정리에서는 이동하거나 삭제하지 않았다.
- Python 3.11 이상. 패키지 의존성은 `pyproject.toml`을 사용한다.

맥에서 현재 기능을 이어받을 때:

```sh
git clone --branch feature/issue-7-common-stock-filter https://github.com/dineyong/SurgeRadar.git SurgeRadar
cd SurgeRadar
```

## 회사 Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m surgeradar replay fixtures/synthetic_iex.jsonl
.\.venv\Scripts\python.exe -m surgeradar universe --universe-fixture fixtures/common_stock_universe.json
```

## 맥북 M1

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m surgeradar replay fixtures/synthetic_iex.jsonl
.venv/bin/python -m surgeradar universe --universe-fixture fixtures/common_stock_universe.json
```

fixture 명령은 네트워크 없이 합성 데이터를 검증하며 실제 계정의 권한을 입증하지 않는다. 맥 M1에서의 실행 검증은 남아 있다.

## 실제 계정 설정과 수신

프로그램은 프로세스 환경변수만 읽으며 `.env`를 자동으로 불러오지 않는다. `.env.example`은 항목과 기본값 참고용이다. API 키를 Git, 명령줄 인수, 로그나 인수인계 문서에 넣지 않는다. README의 Configure explicitly 절차를 참고해 각 기기의 실행 세션에 설정한다.

권한 진단은 `asset-check`, `universe-check`, 스트림 진단은 `check`, 실제 탐지는 `live-detect`다. 실제 호출 전에 현재 계정과 선택한 데이터 feed 권한을 확인한다. 실시간 연결은 두 기기에서 중복 시작하지 않는다. 현재 프로젝트에는 DB, 알림, 주문 기능이 없다.

## 코드와 로그 공유

`.venv`, `.env`와 `logs`는 Git에서 제외된다. 코드와 fixture는 Git으로 공유하고 실제 진단 요약은 비밀 값을 제거해 인수인계에 기록한다. fixture 성공을 실제 시장 데이터 수신 성공이나 수익성 검증으로 기록하지 않는다.
## 작업 시작과 종료

작업 시작 시 저장소 루트에서 아래 명령으로 현재 브랜치와 변경 사항을 확인한다.

```sh
git status --short --branch
git fetch origin
git branch --show-current
```

미커밋 변경이 있으면 먼저 내용을 확인한다. 변경을 버리거나 다른 기기의 작업을 덮어쓰지 않는다. 현재 브랜치가 원하는 작업 브랜치이고 로컬 변경이 정리됐으면 `git pull --ff-only`로 최신 코드를 받는다. 실패하면 분기된 커밋을 확인하고 해결한다. 브랜치는 작업 내용을 확인한 뒤 선택하며, 기존 작업 폴더의 브랜치를 무조건 바꾸지 않는다.

AI에게 작업을 맡길 때는 다음과 같이 요청한다.

> README.md, docs/work-environment.md, docs/handoff.md와 연결된 현황 문서를 읽어줘. 현재 브랜치와 로컬 변경을 확인하고 다음 작업을 진행해줘.

작업 종료 시 `docs/handoff.md`에 변경 내용, 검증 결과, 남은 문제, 다음 작업을 갱신한다. 비밀 값과 원본 운영 로그는 기록하지 않는다. 변경 파일을 검토해 필요한 코드와 문서만 명시적으로 stage하고 커밋한 뒤 해당 작업 브랜치를 push한다. 다른 기기에서 이어가기 전에 push 성공을 확인한다.

`.env`, 가상환경, 설치된 의존성, DB와 실행 중인 프로세스는 Git으로 공유되지 않는다. 맥과 Windows에서 의존성을 각각 설치하고 비밀 값은 각 기기에 따로 설정한다. DB 이력을 이어받으려면 별도의 백업과 복원이 필요하다. MCP를 연결해도 이 데이터가 자동으로 동기화되지는 않는다.
