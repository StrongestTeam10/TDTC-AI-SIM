"""ERD 요약 지표를 이용한 보고서용 차트를 생성한다."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

from app.schemas.report_models import ReportRequest


def configure_korean_font() -> str | None:
    candidates = [
        os.getenv("KOREAN_FONT_PATH"),
        r"C:\Windows\Fonts\malgun.ttf",
        r"C:\Windows\Fonts\malgunsl.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            fm.fontManager.addfont(candidate)
            name = fm.FontProperties(fname=candidate).get_name()
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            return name
    plt.rcParams["axes.unicode_minus"] = False
    return None


def _all_alternatives(request: ReportRequest):
    return [request.baseline, *request.alternatives]


def _figure_height(count: int) -> float:
    """막대 개수에 맞춘 그림 세로 길이(인치).

    가로 막대는 항목이 늘수록 아래로 자란다. 개수에 비례시키되 최소 높이를 두어
    항목이 둘뿐일 때도 제목·범례가 눌리지 않게 한다.
    """

    return min(7.0, max(2.9, 0.95 * count + 1.6))


def _wrap_label(text: str, width: int = 20) -> str:
    """y축 이름표를 여러 줄로 나눈다.

    가로 막대에서는 이름표가 왼쪽에 놓여 기울일 필요가 없지만, 시나리오명이
    "망원시장 화재 시나리오 (2026-07-30 15:37)"처럼 길면 그림 폭을 과하게 잡아먹는다.
    이름과 시각이 자연스럽게 갈라지도록 줄만 나눈다.
    공백이 없는 긴 토큰은 그대로 두어 억지로 잘리지 않게 한다.
    """

    if not text:
        return ""
    lines = textwrap.wrap(text, width=width, break_long_words=False)
    return "\n".join(lines) if lines else text


def _value_labels(values: list[float | int | None], digits: int) -> list[str]:
    """막대 끝에 붙일 값 표기. 산출되지 않은 항목은 빈 문자열로 둔다.

    밀집도가 0.03~0.07처럼 작은 값이라 눈금만으로는 차이를 읽기 어렵다.
    """

    return [
        "" if value is None else f"{float(value):.{digits}f}"
        for value in values
    ]


def create_charts(request: ReportRequest, assets_dir: Path) -> dict[str, str]:
    configure_korean_font()
    assets_dir.mkdir(parents=True, exist_ok=True)
    charts: dict[str, str] = {}
    alternatives = _all_alternatives(request)
    names = [_wrap_label(item.alternative_name) for item in alternatives]

    max_values = [item.metrics.max_density_p_m2 for item in alternatives]
    avg_values = [item.metrics.avg_density_p_m2 for item in alternatives]
    if any(value is not None for value in [*max_values, *avg_values]):
        # 가로 막대를 쓰는 이유: 시나리오명이 길어 세로 막대에서는 이름표를 기울이거나
        # 여러 줄로 접어야 했고, 항목이 2~3개뿐일 때 막대가 넓게 퍼져 휑해 보였다.
        # 가로로 두면 이름표가 왼쪽에 그대로 놓이고 막대 길이 비교도 쉬워진다.
        y = np.arange(len(names))
        height = 0.34
        fig, ax = plt.subplots(figsize=(8.6, _figure_height(len(names))))
        bars_max = ax.barh(
            y - height / 2,
            [
                np.nan if value is None else value
                for value in max_values
            ],
            height,
            label="최대 밀집도",
        )
        bars_avg = ax.barh(
            y + height / 2,
            [
                np.nan if value is None else value
                for value in avg_values
            ],
            height,
            label="평균 밀집도",
        )
        ax.set_xlabel("명/㎡")
        ax.set_title("시나리오별 밀집도 비교")
        ax.set_yticks(y, names)
        # 목록과 같은 순서(기준안이 맨 위)로 읽히도록 뒤집는다.
        ax.invert_yaxis()
        ax.bar_label(bars_max, labels=_value_labels(max_values, 2), padding=3, fontsize=8.5)
        ax.bar_label(bars_avg, labels=_value_labels(avg_values, 2), padding=3, fontsize=8.5)
        # 막대 끝 값 표기가 잘리지 않도록 오른쪽 여유를 둔다.
        peak = max(
            (value for value in [*max_values, *avg_values] if value is not None),
            default=0,
        )
        ax.set_xlim(0, float(peak) * 1.22 if peak else 1)
        ax.legend(loc="lower right")
        fig.tight_layout()
        path = assets_dir / "density_comparison.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        charts["density"] = str(path)

    risk_values = [item.metrics.risk_score for item in alternatives]
    if any(value is not None for value in risk_values):
        y = np.arange(len(names))
        fig, ax = plt.subplots(figsize=(8.6, _figure_height(len(names))))
        bars = ax.barh(
            y,
            [
                np.nan if value is None else value
                for value in risk_values
            ],
            # 계열이 하나뿐이라 밀집도 차트의 두 막대(0.34 x 2)와 비슷한 두께로 맞춘다.
            # 얇게 두면 행 사이 여백만 커져 휑해 보인다.
            0.62,
            color="#C0504D",
        )
        ax.set_xlabel("점")
        ax.set_title("시나리오별 예측 위험점수")
        ax.set_yticks(y, names)
        ax.invert_yaxis()
        ax.bar_label(bars, labels=_value_labels(risk_values, 0), padding=3, fontsize=9)
        peak = max(
            (value for value in risk_values if value is not None), default=0
        )
        ax.set_xlim(0, float(peak) * 1.18 if peak else 1)
        fig.tight_layout()
        path = assets_dir / "risk_score.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        charts["risk"] = str(path)

    if any(
        alternative.density_timeseries
        for alternative in alternatives
    ):
        fig, ax = plt.subplots(
            figsize=(10, 5.2)
        )

        for alternative in alternatives:
            points = alternative.density_timeseries
            if not points:
                continue

            elapsed = [
                point.elapsed_minutes
                for point in points
            ]
            max_density = [
                np.nan
                if point.max_density_p_m2 is None
                else point.max_density_p_m2
                for point in points
            ]
            ax.plot(
                elapsed,
                max_density,
                marker="o",
                linewidth=2,
                label=(
                    f"{alternative.alternative_name} "
                    "최대 밀집도"
                ),
            )

        threshold = (
            request.context
            .density_risk_threshold_p_m2
        )
        if threshold is not None:
            ax.axhline(
                threshold,
                linestyle="--",
                linewidth=1.8,
                color="#D62728",
                label=(
                    f"위험 기준 "
                    f"{threshold:g}명/㎡"
                ),
            )

        ax.set_xlabel("시뮬레이션 경과시간(분)")
        ax.set_ylabel("최대 밀집도(명/㎡)")
        ax.set_title("시간대별 최대 밀집도 변화")
        ax.grid(
            True,
            alpha=0.25,
        )
        ax.legend(
            loc="best",
            fontsize=8.5,
        )
        fig.tight_layout()

        path = (
            assets_dir
            / "density_timeseries.png"
        )
        fig.savefig(
            path,
            dpi=160,
        )
        plt.close(fig)
        charts["density_timeseries"] = str(path)

    return charts
