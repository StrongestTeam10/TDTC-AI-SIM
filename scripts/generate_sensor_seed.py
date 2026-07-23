"""
센서 시드 데이터 생성기.

실제 CCTV/LiDAR/레이더/음향 장비가 없는 개발 단계에서
파이프라인 A를 끝까지 검증하기 위한 가상 실측 데이터를 만든다.

물리적 정합성을 유지한다:
  - 라이다 감지 인원수는 CCTV 집계 인원수와 근사해야 한다 (센서 간 교차 검증)
  - 밀집도가 높아질수록 보행 속도(레이더)는 떨어진다
  - 라이다 포인트 수는 감지 인원수에 비례한다

사용법:
    python -m scripts.generate_sensor_seed > sensor-seed.sql
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

# 구역 정보 (seed-market-data.sql과 일치해야 함)
ZONES = {
    1: {"name": "남측 구역", "area": 471.0},
    2: {"name": "중앙 구역", "area": 588.0},
    3: {"name": "북측 구역", "area": 623.0},
}

# 전통시장 시간대별 방문객 상대 비율 (0.0 ~ 1.0)
# 저녁 장보기 수요로 17~18시가 피크인 국내 전통시장의 통상 패턴을 반영
HOURLY_PATTERN = {
    0: 0.00, 1: 0.00, 2: 0.00, 3: 0.00, 4: 0.00, 5: 0.02,
    6: 0.05, 7: 0.12, 8: 0.18, 9: 0.32, 10: 0.45, 11: 0.58,
    12: 0.62, 13: 0.55, 14: 0.52, 15: 0.68, 16: 0.85, 17: 1.00,
    18: 0.95, 19: 0.72, 20: 0.48, 21: 0.28, 22: 0.10, 23: 0.03,
}

# 요일별 배수 (0=월 ~ 6=일). 주말에 방문객이 늘어난다.
WEEKDAY_MULTIPLIER = {0: 0.85, 1: 0.82, 2: 0.88, 3: 0.90, 4: 1.05, 5: 1.45, 6: 1.30}

# 구역별 최대 동시 체류 인원 기준값 (피크 시각 기준)
ZONE_PEAK_CAPACITY = {1: 260, 2: 340, 3: 330}

# 센서 유형 코드 (comcode01m 기준, 5자 고정: SEN + 2자 약어)
SENSOR_TYPES = {"SENLD": "라이다", "SENRD": "레이더", "SENAU": "음향"}

# 음향 이벤트 종류와 발생 확률 (타임스텝당)
ACOUSTIC_EVENTS = [("비명", 0.0008), ("충돌", 0.0015), ("고성", 0.0035)]

INTERVAL_MINUTES = 10
DAYS = 4
START_DATE = datetime(2026, 7, 20)  # 월요일

# 특수일: 마지막 날은 명절 대목으로 설정한다.
# 평상시 데이터만으로는 밀집도가 LOW 구간에만 머물러 위험 탐지/알림 흐름을
# 검증할 수 없으므로, 실제로 발생하는 과밀 상황을 재현한 날을 포함시킨다.
SPECIAL_DAY_INDEX = 3
SPECIAL_DAY_MULTIPLIER = 4.2
# 특수일 피크(17~18시)에 순간적으로 인파가 몰리는 구간
SURGE_HOURS = {17, 18}
SURGE_ZONE = 2          # 중앙 구역에 병목 발생
SURGE_MULTIPLIER = 1.9


def status_level(density: float) -> str:
    """밀집도(명/m^2)를 혼잡 상태 레벨로 변환. risk.py 임계값과 정합. (crddnst01m.status_level, VARCHAR(10)용)"""
    if density >= 5.0:
        return "CRITICAL"
    if density >= 2.17:
        return "HIGH"
    if density >= 0.72:
        return "MEDIUM"
    return "LOW"


def status_level_code(density: float) -> str:
    """센서 status_level_code(VARCHAR(5))용 공통코드. comcode01m의 LVL01~04와 대응."""
    if density >= 5.0:
        return "LVL04"  # CRITICAL
    if density >= 2.17:
        return "LVL03"  # HIGH
    if density >= 0.72:
        return "LVL02"  # MEDIUM
    return "LVL01"  # LOW


def walking_speed(density: float) -> int:
    """
    밀집도에 따른 평균 보행 속도(cm/s).
    자유 보행 130cm/s에서 시작해 밀집이 심해질수록 30cm/s까지 감소한다.
    """
    if density <= 0.3:
        return random.randint(120, 140)
    speed = 130.0 - (density / 5.0) * 100.0
    return max(25, int(speed + random.uniform(-8, 8)))


def generate() -> str:
    rng = random.Random(20260720)
    random.seed(20260720)
    lines: list[str] = []

    lines.append("-- =========================================")
    lines.append("-- 센서 시드 데이터 (개발/검증용 가상 실측 데이터)")
    lines.append(f"-- 기간: {START_DATE.date()} 부터 {DAYS}일, {INTERVAL_MINUTES}분 간격")
    lines.append("-- 주의: seed-market-data.sql 실행 후에 실행할 것")
    lines.append("-- =========================================")
    lines.append("")

    # 1) 센서 등록: 구역별로 라이다/레이더/음향 각 1대
    lines.append("-- 1) 센서 9대 (구역당 라이다/레이더/음향 각 1대)")
    lines.append("INSERT INTO sensens01m (zone_id, sensor_type_code, ip_address) VALUES")
    rows = []
    sensor_id = 0
    sensor_map: dict[tuple[int, str], int] = {}
    for zid in ZONES:
        for code in SENSOR_TYPES:
            sensor_id += 1
            sensor_map[(zid, code)] = sensor_id
            rows.append(f"    ({zid}, '{code}', '192.168.10.{100 + sensor_id}')")
    lines.append(",\n".join(rows) + ";")
    lines.append("")

    # 2) 시계열 관측 데이터 생성
    crowd_rows: list[str] = []
    lidar_rows: list[str] = []
    radar_rows: list[str] = []
    acoustic_rows: list[str] = []

    steps_per_day = 24 * 60 // INTERVAL_MINUTES
    for day in range(DAYS):
        date = START_DATE + timedelta(days=day)
        is_special = day == SPECIAL_DAY_INDEX
        day_mult = (
            SPECIAL_DAY_MULTIPLIER if is_special
            else WEEKDAY_MULTIPLIER[date.weekday()]
        )

        for step in range(steps_per_day):
            ts = date + timedelta(minutes=step * INTERVAL_MINUTES)
            ratio = HOURLY_PATTERN[ts.hour]
            # 시간대 경계에서 값이 튀지 않도록 다음 시간대와 선형 보간
            next_ratio = HOURLY_PATTERN[(ts.hour + 1) % 24]
            frac = ts.minute / 60.0
            ratio = ratio + (next_ratio - ratio) * frac

            ts_sql = ts.strftime("%Y-%m-%d %H:%M:%S")

            for zid, zinfo in ZONES.items():
                noise = rng.uniform(0.88, 1.12)
                surge = (
                    SURGE_MULTIPLIER
                    if (is_special and ts.hour in SURGE_HOURS and zid == SURGE_ZONE)
                    else 1.0
                )
                count = int(ZONE_PEAK_CAPACITY[zid] * ratio * day_mult * noise * surge)
                count = max(0, count)
                density = count / zinfo["area"]

                crowd_rows.append(
                    f"    ({zid}, {count}, {density:.2f}, "
                    f"'{status_level(density)}', '{ts_sql}')"
                )

                # 라이다: CCTV 집계와 근사하되 센서 특성상 약간의 오차
                lid_cnt = max(0, int(count * rng.uniform(0.92, 1.08)))
                pt_cloud = lid_cnt * rng.randint(180, 260) + rng.randint(500, 1500)
                avg_dist = (
                    int((zinfo["area"] / count) ** 0.5 * 100) if count > 0 else 0
                )  # 1인당 면적의 제곱근 = 평균 인접 거리(cm)
                lidar_rows.append(
                    f"    ({sensor_map[(zid, 'SENLD')]}, {pt_cloud}, '{ts_sql}', "
                    f"{lid_cnt}, {min(avg_dist, 9999)}, '{status_level_code(density)}', "
                    f"{int(density * 100)})"
                )

                # 레이더: 감지 인원수 + 평균 이동 속도
                rad_cnt = max(0, int(count * rng.uniform(0.90, 1.10)))
                radar_rows.append(
                    f"    ({sensor_map[(zid, 'SENRD')]}, {rng.randint(40, 95)}, '{ts_sql}', "
                    f"{rad_cnt}, {walking_speed(density)}, '{status_level_code(density)}')"
                )

                # 음향 이벤트: 사람이 있을 때만, 밀집도가 높을수록 발생 확률 증가
                if count > 20:
                    for sound_type, base_prob in ACOUSTIC_EVENTS:
                        prob = base_prob * (1.0 + density * 2.0)
                        if rng.random() < prob:
                            conf = round(rng.uniform(0.62, 0.97), 2)
                            acoustic_rows.append(
                                f"    ({sensor_map[(zid, 'SENAU')]}, '{sound_type}', "
                                f"{conf}, FALSE, '{ts_sql}')"
                            )

    def emit(title: str, table: str, cols: str, rows: list[str], chunk: int = 500) -> None:
        lines.append(f"-- {title} ({len(rows)}건)")
        for i in range(0, len(rows), chunk):
            part = rows[i : i + chunk]
            lines.append(f"INSERT INTO {table} ({cols}) VALUES")
            lines.append(",\n".join(part) + ";")
        lines.append("")

    emit("2) 인구 밀집도", "crddnst01m",
         "zone_id, visitor_count, density_score, status_level, captured_at", crowd_rows)
    emit("3) 라이다 센서 데이터", "senlidr01m",
         "sensor_id, pt_cloud_cnt, updated_at, detect_cnt, avg_dist_m, status_level_code, density_score",
         lidar_rows)
    emit("4) 레이더 센서 데이터", "senradr01m",
         "sensor_id, refl_intens, updated_at, detect_cnt, avg_speed, status_level_code", radar_rows)
    # 음향 센서 데이터 사용 중단 결정 (audevnt01m/h 테이블 DB에서 제거됨).
    # acoustic_rows 생성 로직은 남겨두되, INSERT문 생성(emit)은 비활성화한다.
    # emit("5) 음향 이벤트", "audevnt01m",
    #      "sensor_id, sound_type, confidence, is_checked, detected_at", acoustic_rows)

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(generate())
