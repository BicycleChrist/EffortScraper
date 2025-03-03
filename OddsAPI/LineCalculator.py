class BestLinesCalculator:
    def __init__(self, table_data, bookmakers):
        self.table_data = table_data
        self.bookmakers = bookmakers

    def calculate_best_lines(self):
        best_lines = {}
        for row_label, row_data in self.table_data.items():
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
            over_lines_by_point = {}  # {point: [(odds, bookmaker), ...]}
            under_lines_by_point = {}  # {point: [(odds, bookmaker), ...]}
    
            # Iterate through bookmakers to collect all lines
            for bm in self.bookmakers:
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
                            over_odds = float(parts[over_idx - 1])
                            point_str = parts[over_idx + 1].strip('()')
                            point = float(point_str)
                            
                            if point not in over_lines_by_point:
                                over_lines_by_point[point] = []
                            over_lines_by_point[point].append((over_odds, bm))
                        except (ValueError, IndexError):
                            print(f"Invalid over odds format for {bm} in {row_label}")
    
                    if 'U' in parts and parts.index('U') > 0 and parts.index('U') + 1 < len(parts):
                        under_idx = parts.index('U')
                        # Validate we have a number before 'U'
                        try:
                            under_odds = float(parts[under_idx - 1])
                            point_str = parts[under_idx + 1].strip('()')
                            point = float(point_str)
                            
                            if point not in under_lines_by_point:
                                under_lines_by_point[point] = []
                            under_lines_by_point[point].append((under_odds, bm))
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
                    
                best_odds, best_bm = max(lines, key=lambda x: x[0])
                # Calculate average ONLY using valid lines at this exact point
                avg_odds = sum(odds for odds, _ in lines) / len(lines)
                
                best_over_by_point[point] = {
                    'odds': best_odds, 
                    'point': point, 
                    'bookmaker': best_bm,
                    'avg_odds': avg_odds,
                    'count': len(lines),
                    'deviation': best_odds - avg_odds
                }
            
            # Find best under line for each point value
            best_under_by_point = {}
            for point, lines in under_lines_by_point.items():
                if not lines:
                    continue
                    
                best_odds, best_bm = max(lines, key=lambda x: x[0])
                # Calculate average ONLY using valid lines at this exact point
                avg_odds = sum(odds for odds, _ in lines) / len(lines)
                
                best_under_by_point[point] = {
                    'odds': best_odds, 
                    'point': point, 
                    'bookmaker': best_bm,
                    'avg_odds': avg_odds,
                    'count': len(lines),
                    'deviation': best_odds - avg_odds
                }
            
            # Find overall best over and under (prioritize by odds)
            best_over = max(best_over_by_point.values(), key=lambda x: x['odds']) if best_over_by_point else None
            best_under = max(best_under_by_point.values(), key=lambda x: x['odds']) if best_under_by_point else None
    
            # Store results
            best_lines[market_key] = {
                'over': best_over,
                'under': best_under,
                'over_by_point': best_over_by_point,
                'under_by_point': best_under_by_point
            }
    
        return best_lines
