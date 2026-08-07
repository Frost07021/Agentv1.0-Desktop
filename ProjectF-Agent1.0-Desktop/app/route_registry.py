from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RouteDefinition:
    route_key: str
    skill_name: str
    input_contract: str
    queue: str
    timeout_seconds: int


class RouteRegistry:
    """产品参数到 Skill 的固定映射；模型无权选择或改写分类。"""

    def __init__(self) -> None:
        self._routes = {
            "report.general": RouteDefinition(
                route_key="report.general",
                skill_name="pet-report-analysis",
                input_contract="image/pdf",
                queue="analysis-image",
                timeout_seconds=120,
            ),
            "home_check.gait": RouteDefinition(
                route_key="home_check.gait",
                skill_name="home-health-check-gait",
                input_contract="video",
                queue="analysis-video",
                timeout_seconds=180,
            ),
            "home_check.behavior": RouteDefinition(
                route_key="home_check.behavior",
                skill_name="home-health-check-behavior",
                input_contract="video",
                queue="analysis-video",
                timeout_seconds=180,
            ),
            "home_check.dental": RouteDefinition(
                route_key="home_check.dental",
                skill_name="home-health-check-dental",
                input_contract="image",
                queue="analysis-image",
                timeout_seconds=120,
            ),
            "home_check.stool": RouteDefinition(
                route_key="home_check.stool",
                skill_name="home-health-check-stool",
                input_contract="image",
                queue="analysis-image",
                timeout_seconds=120,
            ),
            "home_check.xray": RouteDefinition(
                route_key="home_check.xray",
                skill_name="home-health-check-xray",
                input_contract="image/pdf",
                queue="analysis-image",
                timeout_seconds=180,
            ),
        }

    def get(self, route_key: str) -> RouteDefinition:
        try:
            return self._routes[route_key]
        except KeyError as exc:
            raise KeyError(f"不支持的固定业务路由: {route_key}") from exc

    def list(self) -> list[dict[str, object]]:
        return [asdict(route) for route in self._routes.values()]


ROUTES = RouteRegistry()
