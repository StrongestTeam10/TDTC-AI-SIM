"""실제 망원시장 시드 데이터로 시뮬레이션 검증."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.simulation.model import MarketLayout, MarketDigitalTwin, ZoneObservation, SimulationMode

Z = json.load(open('/home/claude/geo/zones.json'))

market_row = {"market_id":1,"market_name":"망원시장","latitude":37.556338,"longitude":126.906131}
zone_rows = [
 {"zone_id":1,"zone_name":"남측 구역","polygon_coordinates":json.dumps({"type":"Polygon","coordinates":[Z["Z1"]["polygon"]]})},
 {"zone_id":2,"zone_name":"중앙 구역","polygon_coordinates":json.dumps({"type":"Polygon","coordinates":[Z["Z2"]["polygon"]]})},
 {"zone_id":3,"zone_name":"북측 구역","polygon_coordinates":json.dumps({"type":"Polygon","coordinates":[Z["Z3"]["polygon"]]})},
]
adjacency_rows = [
 {"from_zone_id":1,"to_zone_id":2,"path_width":6.55,"distance_m":71.8},
 {"from_zone_id":2,"to_zone_id":1,"path_width":6.55,"distance_m":71.8},
 {"from_zone_id":2,"to_zone_id":3,"path_width":6.40,"distance_m":81.4},
 {"from_zone_id":3,"to_zone_id":2,"path_width":6.40,"distance_m":81.4},
]
gate_rows = [
 {"facility_id":1,"name":"Gate 1 (South)","latitude":37.55527435,"longitude":126.90647659},
 {"facility_id":2,"name":"Gate W1","latitude":37.55586876,"longitude":126.90615896},
 {"facility_id":3,"name":"Gate E1","latitude":37.55592302,"longitude":126.90641354},
 {"facility_id":4,"name":"Gate W2","latitude":37.55654207,"longitude":126.90592611},
 {"facility_id":5,"name":"Gate E2","latitude":37.55658508,"longitude":126.90619768},
 {"facility_id":6,"name":"Gate 2 (North)","latitude":37.55744969,"longitude":126.90575803},
]

layout = MarketLayout.from_db_rows(market_row, zone_rows, adjacency_rows, gate_rows)
print("=== 레이아웃 로드 ===")
for zid, s in layout.zones.items():
    print(f"  구역{zid} {s.zone_name}: 면적={s.area_m2:.0f}m2 통로폭={s.path_width_m:.1f}m 출구있음={s.is_exit_zone}")
print(f"  그래프 노드={layout.graph.number_of_nodes()} 엣지={layout.graph.number_of_edges()}")
print(f"  게이트 배정: {[(g['name'], g['zone_id']) for g in layout.gates]}")

print("\n=== 시나리오별 위험도 검증 ===")
scenarios = [
    ("평시 (한산)",      {1: 30,  2: 40,  3: 40}),
    ("주말 오후 (혼잡)",  {1: 300, 2: 400, 3: 400}),
    ("축제 (과밀)",      {1: 900, 2: 1200, 3: 1200}),
    ("특정구역 병목",     {1: 100, 2: 2500, 3: 150}),
]
for label, counts in scenarios:
    obs = {z: ZoneObservation(zone_id=z, visitor_count=c) for z, c in counts.items()}
    m = MarketDigitalTwin(layout, obs, mode=SimulationMode.MIRROR, seed=42)
    snap = m.snapshot()
    print(f"\n[{label}] 종합={snap['overallRiskScore']:.1f}")
    for z in snap["zones"]:
        print(f"   {z['zoneName']}: {z['visitorCount']}명 "
              f"밀집도={z['density']:.2f}명/m2 1인당={z['personalSpace']:.2f}m2 "
              f"→ {z['riskScore']:.1f}({z['riskLevel']})")
    print(f"   에이전트 생성 수: {len(snap['agents'])}")
