# Helper function to get API key from league name
def get_sport_key(sport_name: str, league_name: str) -> str:
    """
    Get the API key for a given sport and league combination.
    Returns None if not found.
    """
    return SPORTS_MARKETS.get(sport_name, {}).get(league_name)



NBA_MARKETS = {
    "player_points": "Points (Over/Under)",
    "player_points_q1": "1st Quarter Points (Over/Under)",
    "player_rebounds": "Rebounds (Over/Under)",
    "player_rebounds_q1": "1st Quarter Rebounds (Over/Under)",
    "player_assists": "Assists (Over/Under)",
    "player_assists_q1": "1st Quarter Assists (Over/Under)",
    "player_threes": "Threes (Over/Under)",
    "player_blocks": "Blocks (Over/Under)",
    "player_steals": "Steals (Over/Under)",
    "player_blocks_steals": "Blocks + Steals (Over/Under)",
    "player_turnovers": "Turnovers (Over/Under)",
    "player_points_rebounds_assists": "Points + Rebounds + Assists (Over/Under)",
    "player_points_rebounds": "Points + Rebounds (Over/Under)",
    "player_points_assists": "Points + Assists (Over/Under)",
    "player_rebounds_assists": "Rebounds + Assists (Over/Under)",
    "player_double_double": "Double Double (Yes/No)",
    "player_triple_double": "Triple Double (Yes/No)",
}

MLB_MARKETS = {
    "batter_home_runs": "Batter home runs (Over/Under)",
    "batter_first_home_run": "Batter first home run (Yes/No)",
    "batter_hits": "Batter hits (Over/Under)",
    "batter_total_bases": "Batter total bases (Over/Under)",
    "batter_rbis": "Batter RBIs (Over/Under)",
    "batter_runs_scored": "Batter runs scored (Over/Under)",
    "batter_hits_runs_rbis": "Batter hits + runs + RBIs (Over/Under)",
    "batter_singles": "Batter singles (Over/Under)",
    "batter_doubles": "Batter doubles (Over/Under)",
    "batter_triples": "Batter triples (Over/Under)",
    "batter_walks": "Batter walks (Over/Under)",
    "batter_strikeouts": "Batter strikeouts (Over/Under)",
    "batter_stolen_bases": "Batter stolen bases (Over/Under)",
    "pitcher_strikeouts": "Pitcher strikeouts (Over/Under)",
    "pitcher_record_a_win": "Pitcher to record a win (Yes/No)",
    "pitcher_hits_allowed": "Pitcher hits allowed (Over/Under)",
    "pitcher_walks": "Pitcher walks (Over/Under)",
    "pitcher_earned_runs": "Pitcher earned runs (Over/Under)",
    "pitcher_outs": "Pitcher outs (Over/Under)",
    # Alternate MLB markets
    "batter_total_bases_alternate": "Alternate batter total bases (Over/Under)",
    "batter_home_runs_alternate": "Alternate batter home runs (Over/Under)",
    "batter_hits_alternate": "Alternate batter hits (Over/Under)",
    "batter_rbis_alternate": "Alternate batter RBIs (Over/Under)",
    "pitcher_hits_allowed_alternate": "Alternate pitcher hits allowed (Over/Under)",
    "pitcher_walks_alternate": "Alternate pitcher walks allowed (Over/Under)",
    "pitcher_strikeouts_alternate": "Alternate pitcher strikeouts (Over/Under)",
}

NHL_MARKETS = {
    "player_points": "Points (Over/Under)",
    "player_power_play_points": "Power play points (Over/Under)",
    "player_assists": "Assists (Over/Under)",
    "player_blocked_shots": "Blocked shots (Over/Under)",
    "player_shots_on_goal": "Shots on goal (Over/Under)",
    "player_goals": "Goals (Over/Under)",
    "player_total_saves": "Total saves (Over/Under)",
    "player_goal_scorer_first": "First Goal Scorer (Yes/No)",
    "player_goal_scorer_last": "Last Goal Scorer (Yes/No)",
    "player_goal_scorer_anytime": "Anytime Goal Scorer (Yes/No)",
    # Alternate NHL markets
    "player_points_alternate": "Alternate Points (Over/Under)",
    "player_assists_alternate": "Alternate Assists (Over/Under)",
    "player_power_play_points_alternate": "Alternate Power Play Points (Over/Under)",
    "player_goals_alternate": "Alternate Goals (Over/Under)",
    "player_shots_on_goal_alternate": "Alternate Shots on Goal (Over/Under)",
    "player_blocked_shots_alternate": "Alternate Blocked Shots (Over/Under)",
    "player_total_saves_alternate": "Alternate Total Saves (Over/Under)",
}

AFL_MARKETS = {
    "player_disposals": "Disposals (Over/Under)",
    "player_disposals_over": "Disposals (Over only)",
    "player_goal_scorer_first": "First Goal Scorer (Yes/No)",
    "player_goal_scorer_last": "Last Goal Scorer (Yes/No)",
    "player_goal_scorer_anytime": "Anytime Goal Scorer (Yes/No)",
    "player_goals_scored_over": "Goals scored (Over only)",
    "player_marks_over": "Marks (Over only)",
    "player_marks_most": "Most Marks (Yes/No)",
    "player_tackles_over": "Tackles (Over only)",
    "player_tackles_most": "Most Tackles (Yes/No)",
    "player_afl_fantasy_points": "AFL Fantasy Points (Over/Under)",
    "player_afl_fantasy_points_over": "AFL Fantasy Points (Over only)",
    "player_afl_fantasy_points_most": "Most AFL Fantasy Points (Yes/No)",
}

SOCCER_MARKETS = {
    "player_goal_scorer_anytime": "Anytime Goal Scorer (Yes/No)",
    "player_first_goal_scorer": "First Goal Scorer (Yes/No)",
    "player_last_goal_scorer": "Last Goal Scorer (Yes/No)",
    "player_to_receive_card": "Player to receive a card (Yes/No)",
    "player_to_receive_red_card": "Player to receive a red card (Yes/No)",
    "player_shots_on_target": "Player Shots on Target (Over/Under)",
    "player_shots": "Player Shots (Over/Under)",
    "player_assists": "Player Assists (Over/Under)",
    "alternate_spreads_corners": "Handicap Corners",
    "alternate_totals_corners": "Total Corners (Over/Under)",
    "alternate_spreads_cards": "Handicap Cards / Bookings",
    "alternate_totals_cards": "Total Cards / Bookings (Over/Under)",
    "double_chance": "Double Chance",
}

# Dictionary mapping sports to their market dictionaries
# Add this to marketKeys.py after the SPORTS_MARKETS dictionary

SPORTS_MARKETS = {
    "American Football": {
        "CFL": "americanfootball_cfl",
        "NCAAF": "americanfootball_ncaaf",
        "NCAAF Championship Winner": "americanfootball_ncaaf_championship_winner",
        "NFL": "americanfootball_nfl",
        "NFL Preseason": "americanfootball_nfl_preseason",
        "NFL Super Bowl Winner": "americanfootball_nfl_super_bowl_winner",
        "UFL": "americanfootball_ufl"
    },
    "Aussie Rules": {
        "AFL": "aussierules_afl"
    },
    "Baseball": {
        "MLB": "baseball_mlb",
        "MLB Preseason": "baseball_mlb_preseason",
        "MLB World Series Winner": "baseball_mlb_world_series_winner",
        "Minor League Baseball": "baseball_milb",
        "NPB": "baseball_npb",
        "KBO League": "baseball_kbo",
        "NCAA Baseball": "baseball_ncaa"
    },
    "Basketball": {
        "Euroleague": "basketball_euroleague",
        "NBA": "basketball_nba",
        "NBA Championship Winner": "basketball_nba_championship_winner",
        "WNBA": "basketball_wnba",
        "NCAAB": "basketball_ncaab",
        "WNCAAB": "basketball_wncaab",
        "NCAAB Championship Winner": "basketball_ncaab_championship_winner",
        "NBL (Australia)": "basketball_nbl"
    },
    "Boxing": {
        "Boxing": "boxing_boxing"
    },
    "Cricket": {
        "Big Bash": "cricket_big_bash",
        "Caribbean Premier League": "cricket_caribbean_premier_league",
        "ICC World Cup": "cricket_icc_world_cup",
        "International Twenty20": "cricket_international_t20",
        "IPL": "cricket_ipl",
        "One Day Internationals": "cricket_odi",
        "Pakistan Super League": "cricket_psl",
        "T20 Blast": "cricket_t20_blast",
        "Test Matches": "cricket_test_match"
    },
    "Golf": {
        "Masters Tournament Winner": "golf_masters_tournament_winner",
        "PGA Championship Winner": "golf_pga_championship_winner",
        "The Open Winner": "golf_the_open_championship_winner",
        "US Open Winner": "golf_us_open_winner"
    },
    "Ice Hockey": {
        "NHL": "icehockey_nhl",
        "NHL Championship Winner": "icehockey_nhl_championship_winner",
        "SHL": "icehockey_sweden_hockey_league",
        "HockeyAllsvenskan": "icehockey_sweden_allsvenskan"
    },
    "Lacrosse": {
        "Premier Lacrosse League": "lacrosse_pll"
    },
    "Mixed Martial Arts": {
        "MMA": "mma_mixed_martial_arts"
    },
    "Politics": {
        "US Presidential Elections Winner": "politics_us_presidential_election_winner"
    },
    "Rugby League": {
        "NRL": "rugbyleague_nrl"
    },
    "Soccer": {
        "Africa Cup of Nations": "soccer_africa_cup_of_nations",
        "Primera División - Argentina": "soccer_argentina_primera_division",
        "A-League": "soccer_australia_aleague",
        "Austrian Football Bundesliga": "soccer_austria_bundesliga",
        "Belgium First Div": "soccer_belgium_first_div",
        "Brazil Série A": "soccer_brazil_campeonato",
        "Brazil Série B": "soccer_brazil_serie_b",
        "Primera División - Chile": "soccer_chile_campeonato",
        "Super League - China": "soccer_china_superleague",
        "Denmark Superliga": "soccer_denmark_superliga",
        "Championship": "soccer_efl_champ",
        "EFL Cup": "soccer_england_efl_cup",
        "League 1": "soccer_england_league1",
        "League 2": "soccer_england_league2",
        "EPL": "soccer_epl",
        "FA Cup": "soccer_fa_cup",
        "FIFA World Cup": "soccer_fifa_world_cup",
        "FIFA Women's World Cup": "soccer_fifa_world_cup_womens",
        "FIFA World Cup Winner": "soccer_fifa_world_cup_winner",
        "Veikkausliiga - Finland": "soccer_finland_veikkausliiga",
        "Ligue 1 - France": "soccer_france_ligue_one",
        "Ligue 2 - France": "soccer_france_ligue_two",
        "Bundesliga - Germany": "soccer_germany_bundesliga",
        "Bundesliga 2 - Germany": "soccer_germany_bundesliga2",
        "3. Liga - Germany": "soccer_germany_liga3",
        "Super League - Greece": "soccer_greece_super_league",
        "Serie A - Italy": "soccer_italy_serie_a",
        "Serie B - Italy": "soccer_italy_serie_b",
        "J League": "soccer_japan_j_league",
        "K League 1": "soccer_korea_kleague1",
        "League of Ireland": "soccer_league_of_ireland",
        "Liga MX": "soccer_mexico_ligamx",
        "Dutch Eredivisie": "soccer_netherlands_eredivisie",
        "Eliteserien - Norway": "soccer_norway_eliteserien",
        "Ekstraklasa - Poland": "soccer_poland_ekstraklasa",
        "Primeira Liga - Portugal": "soccer_portugal_primeira_liga",
        "La Liga - Spain": "soccer_spain_la_liga",
        "La Liga 2 - Spain": "soccer_spain_segunda_division",
        "Premiership - Scotland": "soccer_spl",
        "Allsvenskan - Sweden": "soccer_sweden_allsvenskan",
        "Superettan - Sweden": "soccer_sweden_superettan",
        "Swiss Superleague": "soccer_switzerland_superleague",
        "Turkey Super League": "soccer_turkey_super_league",
        "UEFA Europa Conference League": "soccer_uefa_europa_conference_league",
        "UEFA Champions League": "soccer_uefa_champs_league",
        "UEFA Champions League Qualification": "soccer_uefa_champs_league_qualification",
        "UEFA Europa League": "soccer_uefa_europa_league",
        "UEFA Euro 2024": "soccer_uefa_european_championship",
        "UEFA Euro Qualification": "soccer_uefa_euro_qualification",
        "UEFA Nations League": "soccer_uefa_nations_league",
        "Copa América": "soccer_conmebol_copa_america",
        "Copa Libertadores": "soccer_conmebol_copa_libertadores",
        "MLS": "soccer_usa_mls"
    },
    "Tennis": {
        "ATP Australian Open": "tennis_atp_aus_open_singles",
        "ATP Canadian Open": "tennis_atp_canadian_open",
        "ATP China Open": "tennis_atp_china_open",
        "ATP Cincinnati Open": "tennis_atp_cincinnati_open",
        "ATP French Open": "tennis_atp_french_open",
        "ATP Paris Masters": "tennis_atp_paris_masters",
        "ATP Shanghai Masters": "tennis_atp_shanghai_masters",
        "ATP US Open": "tennis_atp_us_open",
        "ATP Wimbledon": "tennis_atp_wimbledon",
        "WTA Australian Open": "tennis_wta_aus_open_singles",
        "WTA Canadian Open": "tennis_wta_canadian_open",
        "WTA China Open": "tennis_wta_china_open",
        "WTA Cincinnati Open": "tennis_wta_cincinnati_open",
        "WTA French Open": "tennis_wta_french_open",
        "WTA US Open": "tennis_wta_us_open",
        "WTA Wimbledon": "tennis_wta_wimbledon",
        "WTA Wuhan Open": "tennis_wta_wuhan_open"
    }
}


