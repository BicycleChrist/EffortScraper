def american_to_decimal(american_odds):
    """Convert American odds to decimal format for calculation purposes"""
    if american_odds > 0:
        return (american_odds / 100) + 1
    else:
        return (100 / abs(american_odds)) + 1

def decimal_to_american(decimal_odds):
    """Convert decimal odds back to American format for display"""
    if decimal_odds >= 2:
        return round((decimal_odds - 1) * 100)
    else:
        return round(-100 / (decimal_odds - 1))

def calculate_best_lines(table_data, bookmakers):
    best_lines = {}
    for row_label, row_data in table_data.items():
        if row_data.get('is_header'):
            continue  # Skip header rows
    
        # Parse player name and market type from row label
        parts = row_label.split(' - ')
        if len(parts) < 2:
            continue
        player_name = parts[0]
        market_type = parts[1]
        market_key = f"{player_name}:{market_type}"
    
        # Group odds by point value
        over_lines_by_point = {}  # {point: [(decimal_odds, american_odds, bookmaker), ...]}
        under_lines_by_point = {}  # {point: [(decimal_odds, american_odds, bookmaker), ...]}
    
        # Iterate through bookmakers to collect all lines
        for bm in bookmakers:
            if bm not in row_data:
                continue
            
            # Skip empty cells explicitly
            value = row_data[bm]
            if not value or value.strip() == "":
                continue
    
            # Parse over and under odds
            try:
                parts = value.split()
                
                # Only process if we have enough parts for valid data
                if 'O' in parts and parts.index('O') > 0 and parts.index('O') + 1 < len(parts):
                    over_idx = parts.index('O')
                    # Validate we have a number before 'O'
                    try:
                        # Convert American odds to float and then to decimal odds
                        american_odds = float(parts[over_idx - 1])
                        decimal_odds = american_to_decimal(american_odds)
                        
                        point_str = parts[over_idx + 1].strip('()')
                        point = float(point_str)
                        
                        if point not in over_lines_by_point:
                            over_lines_by_point[point] = []
                        
                        # Store the american odds for reference along with the decimal odds for calculations
                        over_lines_by_point[point].append((decimal_odds, american_odds, bm))
                    except (ValueError, IndexError):
                        print(f"Invalid over odds format for {bm} in {row_label}")
    
                if 'U' in parts and parts.index('U') > 0 and parts.index('U') + 1 < len(parts):
                    under_idx = parts.index('U')
                    # Validate we have a number before 'U'
                    try:
                        # Convert American odds to float and then to decimal odds
                        american_odds = float(parts[under_idx - 1])
                        decimal_odds = american_to_decimal(american_odds)
                        
                        point_str = parts[under_idx + 1].strip('()')
                        point = float(point_str)
                        
                        if point not in under_lines_by_point:
                            under_lines_by_point[point] = []
                        
                        # Store the american odds for reference along with the decimal odds for calculations
                        under_lines_by_point[point].append((decimal_odds, american_odds, bm))
                    except (ValueError, IndexError):
                        print(f"Invalid under odds format for {bm} in {row_label}")
            except Exception as e:
                print(f"Error parsing odds for {row_label}, bookmaker {bm}: {e}")
                continue
    
        if not over_lines_by_point and not under_lines_by_point:
            continue  # Skip if no lines found
    
        # Find best over line for each point value
        best_over_by_point = {}
        for point, lines in over_lines_by_point.items():
            if not lines:
                continue
                
            # Best odds are max decimal odds
            best_decimal_odds, american_odds, best_bm = max(lines, key=lambda x: x[0])
            
            # Calculate average in decimal format
            avg_decimal_odds = sum(dec_odds for dec_odds, _, _ in lines) / len(lines)
            
            # Calculate deviation as a percentage difference in implied probability
            implied_prob_best = 1 / best_decimal_odds
            implied_prob_avg = 1 / avg_decimal_odds
            prob_advantage = implied_prob_avg - implied_prob_best
            
            # Express as percentage points advantage (positive is good)
            deviation_percentage = prob_advantage * 100
            
            best_over_by_point[point] = {
                'odds': american_odds,  # Keep the original American odds
                'point': point, 
                'bookmaker': best_bm,
                'avg_odds': decimal_to_american(avg_decimal_odds),  # Convert average back to American
                'count': len(lines),
                'deviation': round(deviation_percentage, 2)  # Percentage point edge
            }
        
        # Find best under line for each point value
        best_under_by_point = {}
        for point, lines in under_lines_by_point.items():
            if not lines:
                continue
                
            # Best odds are max decimal odds
            best_decimal_odds, american_odds, best_bm = max(lines, key=lambda x: x[0])
            
            # Calculate average in decimal format
            avg_decimal_odds = sum(dec_odds for dec_odds, _, _ in lines) / len(lines)
            
            # Calculate deviation as a percentage difference in implied probability
            implied_prob_best = 1 / best_decimal_odds
            implied_prob_avg = 1 / avg_decimal_odds
            prob_advantage = implied_prob_avg - implied_prob_best
            
            # Express as percentage points advantage (positive is good)
            deviation_percentage = prob_advantage * 100
            
            best_under_by_point[point] = {
                'odds': american_odds,  # Keep the original American odds
                'point': point, 
                'bookmaker': best_bm,
                'avg_odds': decimal_to_american(avg_decimal_odds),  # Convert average back to American
                'count': len(lines),
                'deviation': round(deviation_percentage, 2)  # Percentage point edge
            }
        
        # Find overall best over and under (prioritize by odds in decimal format for comparison)
        best_over = None
        if best_over_by_point:
            # Convert to list of tuples for easier sorting
            over_points = [(point, data) for point, data in best_over_by_point.items()]
            # Sort by odds (converting American to decimal first)
            over_points.sort(key=lambda x: american_to_decimal(x[1]['odds']), reverse=True)
            best_over = over_points[0][1]
        
        best_under = None
        if best_under_by_point:
            # Convert to list of tuples for easier sorting
            under_points = [(point, data) for point, data in best_under_by_point.items()]
            # Sort by odds (converting American to decimal first)
            under_points.sort(key=lambda x: american_to_decimal(x[1]['odds']), reverse=True)
            best_under = under_points[0][1]
    
        # Store results
        best_lines[market_key] = {
            'over': best_over,
            'under': best_under,
            'over_by_point': best_over_by_point,
            'under_by_point': best_under_by_point
        }
    
    return best_lines
