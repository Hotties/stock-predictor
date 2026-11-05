# stock-predictor

주식 다음 거래일 등락률 예측 웹사이트. 설계는 「주식 가격 예측 웹사이트 – 설계 문서」, 코드 구성은 「코드 흐름도」를 따른다.

## 현재 단계: M1 (1단계 완료)

| 단계 | 내용 | 상태 |
| --- | --- | --- |
| 1 | 설정, KST 시각, DB 연결·모델·읽기쓰기 | 작성 완료 (DB 층은 사용자 PC에서 테스트 필요) |
| 2 | 달력(`services/calendar.py`) | 예정 |
| 3 | 수집기(`collectors/*`) | 예정 |
| 4 | 지원 판정(`services/support.py`) | 예정 |
| 5 | 편입·갱신(`services/onboarding.py`, `refresh.py`) | 예정 |
| 6 | 작업 진입점(`jobs/*`) | 예정 |

## 준비

```
pip install -r requirements.txt
cp config/settings.example.toml config/settings.toml      # 필요하면 값 수정
export STOCK_DB_PASSWORD=...                                # DB 비밀번호는 환경변수로만
mysql -u <user> -p <db> < sql/001_create_tables.sql         # 테이블 생성
```

## 테스트

프로젝트 폴더에서 실행한다. 외부 접속이 없는 테스트이고 MariaDB도 필요 없다(DB 테스트는 메모리 SQLite).

```
python -m unittest discover -s tests -t . -v
```

SQLAlchemy가 설치되어 있지 않으면 DB 테스트(`test_db_repo.py`)는 건너뛴다.
