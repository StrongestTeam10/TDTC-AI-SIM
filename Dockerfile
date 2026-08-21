FROM python:3.12-slim

WORKDIR /app

# shapely/psycopg2 빌드에 필요한 시스템 패키지
#
# fonts-nanum (2026-08-20 추가): 보고서 차트의 한글이 서버에서만 □□□로 깨졌다.
# python:3.12-slim 에는 한글 글리프를 가진 폰트가 하나도 없어 matplotlib이
# DejaVu Sans로 대체하기 때문이다(숫자·날짜만 멀쩡하고 한글만 깨지는 증상).
# charting.py의 configure_korean_font()가 후보로 찾는 경로
# /usr/share/fonts/truetype/nanum/NanumGothic.ttf 가 이 패키지로 생긴다.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libgeos-dev libpq-dev fonts-nanum \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
