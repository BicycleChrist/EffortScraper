import math
import json
from typing import Dict, List, Tuple

def convert_american_to_implied_prob(american_odds: int) -> float:
    """Convert American odds to implied probability"""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    else:
        return (-american_odds) / (-american_odds + 100)

def calculate_edge(pinnacle_odds: int, dfs_odds: int = -137) -> float:
    """
    Calculate the edge between Pinnacle's line and DFS fixed odds
    """
    pinnacle_prob = convert_american_to_implied_prob(pinnacle_odds)
    dfs_prob = convert_american_to_implied_prob(dfs_odds)
    return (pinnacle_prob - dfs_prob) * 100

def find_best_parlays(comparison_results: Dict, dfs_site: str = 'PrizePicks') -> Tuple[List, List]:
    """
    Analyze comparison results to find the best 2-leg and 3-leg parlay opportunities.
    """
    #print("I'll launch computer out in culdesac once this works")
    opportunities = []
    
    print(f"Processing parlays for {dfs_site}...")
    
    
    
    for player, sources in comparison_results.items():
        print(f"\nDEBUG - {player} data: {json.dumps(sources, indent=2)}")
        pinnacle_props = sources.get('pinnacle', {})
        dfs_props = sources.get('dfs', {})

        if not pinnacle_props or not dfs_props:
            continue
        
        print(f"DEBUG - {player} has DFS books: {list(dfs_props.keys())}")

        for book, props_list in dfs_props.items():
            print(f"DEBUG - Checking DFS book: {book}")
            if book != dfs_site:
                continue

            for dfs_prop in props_list:
                dfs_line = dfs_prop.get('line')
                if dfs_line is None:
                    continue

                for pinn_prop in pinnacle_props.get('Pinnacle', []):
                    pinn_line = pinn_prop.get('line')
                    
                    print(f"DEBUG - Comparing {player}: Pinnacle {pinn_line} ({pinn_prop['odds']}), DFS {dfs_line} ({dfs_prop['odds']})")
                    
                    if pinn_line != dfs_line:
                        print(f"SKIPPING {player}: Pinnacle line {pinn_line} != DFS line {dfs_line}")
                        continue
                    if pinn_prop['type'] != dfs_prop['type']:
                        print(f"SKIPPING {player}: Pinnacle type {pinn_prop['type']} != DFS type {dfs_prop['type']}")
                        continue
                    
                    # Calculate edge using both Pinnacle and DFS odds
                    edge = calculate_edge(pinn_prop['odds'], dfs_prop['odds'])
                    print(f"DEBUG - Edge calculation for {player}: Pinnacle {pinn_prop['odds']}, DFS {dfs_prop['odds']}, Edge {edge:.2f}%")
                    
                    if abs(edge) >= 0.01:
                        print(f"Opportunity found: {player}, {dfs_prop['market']}, DFS Line {dfs_line}, Pinnacle Line {pinn_line}, {dfs_prop['type']}, {pinn_prop['type']}, Pinnacle Odds: {pinn_prop['odds']}, Edge: {edge:.1f}%")
                        opportunities.append({
                            'player': player,
                            'market': dfs_prop['market'],
                            'line': dfs_line,
                            'side': dfs_prop['type'],
                            'edge': edge,
                            'pinnacle_odds': pinn_prop['odds']
                        })
    
    print(f"Total valid opportunities found: {len(opportunities)}")
    print("Opportunities:", json.dumps(opportunities, indent=2))  # Debugging print
    
    opportunities.sort(key=lambda x: abs(x['edge']), reverse=True)
    best_two_leg = opportunities[:2] if len(opportunities) >= 2 else []
    best_three_leg = opportunities[:3] if len(opportunities) >= 3 else []
    
    return best_two_leg, best_three_leg

def format_parlay_results(two_leg: List, three_leg: List, dfs_site: str) -> str:
    """Format the parlay results into a readable string"""
    output = []
    output.append(f"\nBest {dfs_site} Parlays Based on Pinnacle Lines:")
    output.append("-" * 50)
    
    if two_leg:
        output.append("\n2-Leg Parlay Recommendation:")
        ev = 1.0
        for play in two_leg:
            ev *= convert_american_to_implied_prob(play['pinnacle_odds'])
            output.append(
                f"  {play['player']} {play['market'].replace('player_', '')} "
                f"{play['line']} {play['side']} (Edge: {play['edge']:.1f}%)"
            )
        output.append(f"  Combined Edge vs. -137 Each: {((ev - convert_american_to_implied_prob(-137) ** 2) * 100):.1f}%")
    
    if three_leg:
        output.append("\n3-Leg Parlay Recommendation:")
        ev = 1.0
        for play in three_leg:
            ev *= convert_american_to_implied_prob(play['pinnacle_odds'])
            output.append(
                f"  {play['player']} {play['market'].replace('player_', '')} "
                f"{play['line']} {play['side']} (Edge: {play['edge']:.1f}%)"
            )
        output.append(f"  Combined Edge vs. -137 Each: {((ev - convert_american_to_implied_prob(-137) ** 3) * 100):.1f}%")
    
    return "\n".join(output)
