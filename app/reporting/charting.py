"""ERD 요약 지표를 이용한 보고서용 차트를 생성한다."""

from __future__ import annotations

import os
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


def create_charts(request: ReportRequest, assets_dir: Path) -> dict[str, str]:
    configure_korean_font()
    assets_dir.mkdir(parents=True, exist_ok=True)
    charts: dict[str, str] = {}
    alternatives = _all_alternatives(request)
    names = [item.alternative_name for item in alternatives]

    max_values = [item.metrics.max_density_p_m2 for item in alternatives]
    avg_values = [item.metrics.avg_density_p_m2 for item in alternatives]
    if any(value is not None for value in [*max_values, *avg_values]):
        x = np.arange(len(names))
        width = 0.36
        fig, ax = plt.subplots(figsize=(9, 4.8))
        ax.bar(
            x - width / 2,
            [
                np.nan if value is None else value
                for value in max_values
            ],
            width,
            label="최대 밀집도",
        )
        ax.bar(
            x + width / 2,
            [
                np.nan if value is None else value
                for value in avg_values
            ],
            width,
            label="평균 밀집도",
        )
        ax.set_ylabel("명/㎡")
        ax.set_title("시나리오별 밀집도 비교")
        ax.set_xticks(x, names, rotation=15, ha="right")
        ax.legend()
        fig.tight_layout()
        path = assets_dir / "density_comparison.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        charts["density"] = str(path)

    risk_values = [item.metrics.risk_score for item in alternatives]
    if any(value is not None for value in risk_values):
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.bar(
            names,
            [
                np.nan if value is None else value
                for value in risk_values
            ],
        )
        ax.set_ylim(bottom=0)
        ax.set_ylabel("점")
        ax.set_title("시나리오별 예측 위험점수")
        ax.tick_params(axis="x", rotation=15)
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
