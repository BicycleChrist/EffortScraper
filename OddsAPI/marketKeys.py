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
    "player_field_goals": "Field Goals Made (Over/Under)",
    "player_frees_made": "Free Throws Made (Over/Under)",
    "player_frees_attempts": "Free Throw Attempts (Over/Under)",
    "player_first_basket": "First Basket Scorer (Yes/No)",
    "player_first_team_basket": "First Team Basket Scorer (Yes/No)",
    "player_method_of_first_basket": "Method of First Basket (Yes/No)",
    "player_fantasy_points": "Fantasy Points (Over/Under)",
    # Alternate NBA markets
    "player_points_alternate": "Alternate Points (Over/Under)",
    "player_rebounds_alternate": "Alternate Rebounds (Over/Under)",
    "player_assists_alternate": "Alternate Assists (Over/Under)",
    "player_blocks_alternate": "Alternate Blocks (Over/Under)",
    "player_steals_alternate": "Alternate Steals (Over/Under)",
    "player_turnovers_alternate": "Alternate Turnovers (Over/Under)",
    "player_threes_alternate": "Alternate Threes (Over/Under)",
    "player_points_assists_alternate": "Alternate Points + Assists (Over/Under)",
    "player_points_rebounds_alternate": "Alternate Points + Rebounds (Over/Under)",
    "player_rebounds_assists_alternate": "Alternate Rebounds + Assists (Over/Under)",
    "player_points_rebounds_assists_alternate": "Alternate Points + Rebounds + Assists (Over/Under)",
    "player_fantasy_points_alternate": "Alternate Fantasy Points (Over/Under)",
}


NFL_MARKETS = {
    "player_assists": "Assists (Over/Under)",
    "player_defensive_interceptions": "Defensive Interceptions (Over/Under)",
    "player_field_goals": "Field Goals (Over/Under)",
    "player_kicking_points": "Kicking Points (Over/Under)",
    "player_pass_attempts": "Pass Attempts (Over/Under)",
    "player_pass_completions": "Pass Completions (Over/Under)",
    "player_pass_interceptions": "Pass Intercepts (Over/Under)",
    "player_pass_longest_completion": "Longest Pass Completion (Over/Under)",
    "player_pass_rush_reception_tds": "Pass + Rush + Reception Touchdowns (Over/Under)",
    "player_pass_rush_reception_yds": "Pass + Rush + Reception Yards (Over/Under)",
    "player_pass_tds": "Pass Touchdowns (Over/Under)",
    "player_pass_yds": "Pass Yards (Over/Under)",
    "player_pass_yds_q1": "1st Quarter Pass Yards (Over/Under)",
    "player_pats": "Points After Touchdown (Over/Under)",
    "player_receptions": "Receptions (Over/Under)",
    "player_reception_longest": "Longest Reception (Over/Under)",
    "player_reception_tds": "Reception Touchdowns (Over/Under)",
    "player_reception_yds": "Reception Yards (Over/Under)",
    "player_rush_attempts": "Rush Attempts (Over/Under)",
    "player_rush_longest": "Longest Rush (Over/Under)",
    "player_rush_reception_tds": "Rush + Reception Touchdowns (Over/Under)",
    "player_rush_reception_yds": "Rush + Reception Yards (Over/Under)",
    "player_rush_tds": "Rush Touchdowns (Over/Under)",
    "player_rush_yds": "Rush Yards (Over/Under)",
    "player_sacks": "Sacks (Over/Under)",
    "player_solo_tackles": "Solo Tackles (Over/Under)",
    "player_tackles_assists": "Tackles + Assists (Over/Under)",
    "player_tds_over": "Touchdowns (Over only)",
    "player_1st_td": "1st Touchdown Scorer (Yes/No)",
    "player_anytime_td": "Anytime Touchdown Scorer (Yes/No)",
    "player_last_td": "Last Touchdown Scorer (Yes/No)",
    "player_pass_rush_yds": "Pass + Rush Yards (Over/Under)",
    # Alternate NFL markets
    "player_assists_alternate": "Alternate Assists (Over/Under)",
    "player_field_goals_alternate": "Alternate Field Goals (Over/Under)",
    "player_kicking_points_alternate": "Alternate Kicking Points (Over/Under)",
    "player_pass_attempts_alternate": "Alternate Pass Attempts (Over/Under)",
    "player_pass_completions_alternate": "Alternate Pass Completions (Over/Under)",
    "player_pass_interceptions_alternate": "Alternate Pass Interceptions (Over/Under)",
    "player_pass_longest_completion_alternate": "Alternate Longest Pass Completion (Over/Under)",
    "player_pass_rush_yds_alternate": "Alternate Pass + Rush Yards (Over/Under)",
    "player_pass_rush_reception_tds_alternate": "Alternate Pass + Rush + Reception Touchdowns (Over/Under)",
    "player_pass_rush_reception_yds_alternate": "Alternate Pass + Rush + Reception Yards (Over/Under)",
    "player_pass_tds_alternate": "Alternate Pass Touchdowns (Over/Under)",
    "player_pass_yds_alternate": "Alternate Pass Yards (Over/Under)",
    "player_pats_alternate": "Alternate Points After Touchdown (Over/Under)",
    "player_receptions_alternate": "Alternate Receptions (Over/Under)",
    "player_reception_longest_alternate": "Alternate Longest Reception (Over/Under)",
    "player_reception_tds_alternate": "Alternate Reception Touchdowns (Over/Under)",
    "player_reception_yds_alternate": "Alternate Reception Yards (Over/Under)",
    "player_rush_attempts_alternate": "Alternate Rush Attempts (Over/Under)",
    "player_rush_longest_alternate": "Alternate Longest Rush (Over/Under)",
    "player_rush_reception_tds_alternate": "Alternate Rush + Reception Touchdowns (Over/Under)",
    "player_rush_reception_yds_alternate": "Alternate Rush + Reception Yards (Over/Under)",
    "player_rush_tds_alternate": "Alternate Rush Touchdowns (Over/Under)",
    "player_rush_yds_alternate": "Alternate Rush Yards (Over/Under)",
    "player_sacks_alternate": "Alternate Sacks (Over/Under)",
    "player_solo_tackles_alternate": "Alternate Solo Tackles (Over/Under)",
    "player_tackles_assists_alternate": "Alternate Tackles + Assists (Over/Under)",
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
    "batter_fantasy_score": "Batter fantasy score (Over/Under)",
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
    "batter_walks_alternate": "Alternate batter walks (Over/Under)",
    "batter_strikeouts_alternate": "Alternate batter strikeouts (Over/Under)",
    "batter_runs_scored_alternate": "Alternate batter runs scored (Over/Under)",
    "batter_hits_runs_rbis_alternate": "Alternate batter hits + runs + RBIs (Over/Under)",
    "batter_singles_alternate": "Alternate batter singles (Over/Under)",
    "batter_doubles_alternate": "Alternate batter doubles (Over/Under)",
    "batter_triples_alternate": "Alternate batter triples (Over/Under)",
    "batter_fantasy_score_alternate": "Alternate batter fantasy score (Over/Under)",
    "pitcher_hits_allowed_alternate": "Alternate pitcher hits allowed (Over/Under)",
    "pitcher_walks_alternate": "Alternate pitcher walks allowed (Over/Under)",
    "pitcher_earned_runs_alternate": "Alternate pitcher earned runs (Over/Under)",
    "pitcher_strikeouts_alternate": "Alternate pitcher strikeouts (Over/Under)",
    "pitcher_outs_alternate": "Alternate pitcher outs (Over/Under)",
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
    "player_clearances_over": "Clearances (Over only)",
    "player_kicks_over": "Kicks (Over only)",
    "player_handballs_over": "Handballs (Over only)",
}

RUGBY_LEAGUE_MARKETS = {
    "player_try_scorer_first": "First Try Scorer (Yes/No)",
    "player_try_scorer_last": "Last Try Scorer (Yes/No)",
    "player_try_scorer_anytime": "Anytime Try Scorer (Yes/No)",
    "player_try_scorer_over": "Try Scorer (Over only)",
}

SOCCER_MARKETS = {
    # Player props
    "player_goal_scorer_anytime": "Anytime Goal Scorer (Yes/No)",
    "player_first_goal_scorer": "First Goal Scorer (Yes/No)",
    "player_last_goal_scorer": "Last Goal Scorer (Yes/No)",
    "player_to_receive_card": "Player to receive a card (Yes/No)",
    "player_to_receive_red_card": "Player to receive a red card (Yes/No)",
    "player_shots_on_target": "Player Shots on Target (Over/Under)",
    "player_shots": "Player Shots (Over/Under)",
    "player_assists": "Player Assists (Over/Under)",
    # Game markets
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
        "NBL (Australia)": "basketball_nbl",
        "NBA Preseason": "basketball_nba_preseason",
        "NBA Summer League": "basketball_nba_summer_league",
        "NBA All Star": "basketball_nba_all_stars"
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
        "Test Matches": "cricket_test_match",
        "Asia Cup": "cricket_asia_cup",
        "ICC Champions Trophy": "cricket_icc_trophy",
        "ICC Women's World Cup": "cricket_icc_world_cup_womens",
        "T20 World Cup": "cricket_t20_world_cup",
        "The Hundred": "cricket_the_hundred"
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
        "HockeyAllsvenskan": "icehockey_sweden_allsvenskan",
        "AHL": "icehockey_ahl",
        "Liiga": "icehockey_liiga",
        "Mestis": "icehockey_mestis",
        "NHL Preseason": "icehockey_nhl_preseason"
    },
    "Lacrosse": {
        "Premier Lacrosse League": "lacrosse_pll",
        "NCAA Lacrosse": "lacrosse_ncaa"
    },
    "Mixed Martial Arts": {
        "MMA": "mma_mixed_martial_arts"
    },
    "Politics": {
        "US Presidential Elections Winner": "politics_us_presidential_election_winner"
    },
    "Rugby League": {
        "NRL": "rugbyleague_nrl",
        "State of Origin": "rugbyleague_nrl_state_of_origin"
    },
    "Rugby Union": {
        "Six Nations": "rugbyunion_six_nations"
    },
    "Handball": {
        "Handball-Bundesliga": "handball_germany_bundesliga"
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
        "MLS": "soccer_usa_mls",
        "Saudi Pro League": "soccer_saudi_arabia_pro_league",
        "Premier League - Russia": "soccer_russia_premier_league",
        "DFB-Pokal": "soccer_germany_dfb_pokal",
        "Coppa Italia": "soccer_italy_coppa_italia",
        "Copa del Rey": "soccer_spain_copa_del_rey",
        "Coupe de France": "soccer_france_coupe_de_france",
        "Leagues Cup": "soccer_concacaf_leagues_cup",
        "CONCACAF Gold Cup": "soccer_concacaf_gold_cup",
        "Copa Sudamericana": "soccer_conmebol_copa_sudamericana",
        "FIFA Club World Cup": "soccer_fifa_club_world_cup",
        "FIFA World Cup Qualifiers - Europe": "soccer_fifa_world_cup_qualifiers_europe",
        "FIFA World Cup Qualifiers - South America": "soccer_fifa_world_cup_qualifiers_south_america",
        "Frauen-Bundesliga": "soccer_germany_bundesliga_women",
        "UEFA Champions League Women": "soccer_uefa_champs_league_women"
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
        "WTA Wuhan Open": "tennis_wta_wuhan_open",
        "ATP Indian Wells": "tennis_atp_indian_wells",
        "ATP Miami Open": "tennis_atp_miami_open",
        "ATP Monte-Carlo Masters": "tennis_atp_monte_carlo_masters",
        "ATP Madrid Open": "tennis_atp_madrid_open",
        "ATP Italian Open": "tennis_atp_italian_open",
        "ATP Barcelona Open": "tennis_atp_barcelona_open",
        "ATP Hamburg Open": "tennis_atp_hamburg_open",
        "ATP Munich": "tennis_atp_munich",
        "ATP Dubai": "tennis_atp_dubai",
        "ATP Qatar Open": "tennis_atp_qatar_open",
        "WTA Indian Wells": "tennis_wta_indian_wells",
        "WTA Miami Open": "tennis_wta_miami_open",
        "WTA Madrid Open": "tennis_wta_madrid_open",
        "WTA Italian Open": "tennis_wta_italian_open",
        "WTA Charleston Open": "tennis_wta_charleston_open",
        "WTA Stuttgart Open": "tennis_wta_stuttgart_open",
        "WTA Dubai Championships": "tennis_wta_dubai",
        "WTA Qatar Open": "tennis_wta_qatar_open",
        "WTA Internationaux de Strasbourg": "tennis_wta_strasbourg",
        "WTA Queen's Club Championships": "tennis_wta_queens_club_champ"
    }
}

# Player-prop market set per league. A prop set is shared by every league of the
# same sport (e.g. NFL props also apply to NCAAF/CFL/UFL), so map each table to
# all relevant sport keys. Built programmatically so it stays in sync with
# SPORTS_MARKETS and can't drift on a typo (the old "football_nfl" key never
# matched the real sport key "americanfootball_nfl", so NFL props never loaded).
_PROP_TABLE_LEAGUES = [
    (NFL_MARKETS, ["americanfootball_nfl", "americanfootball_nfl_preseason",
                   "americanfootball_ncaaf", "americanfootball_cfl",
                   "americanfootball_ufl"]),
    (NBA_MARKETS, ["basketball_nba", "basketball_wnba", "basketball_ncaab",
                   "basketball_wncaab"]),
    (MLB_MARKETS, ["baseball_mlb", "baseball_mlb_preseason"]),
    (NHL_MARKETS, ["icehockey_nhl"]),
    (AFL_MARKETS, ["aussierules_afl"]),
    (RUGBY_LEAGUE_MARKETS, ["rugbyleague_nrl"]),
    # Soccer props apply to every soccer league we track
    (SOCCER_MARKETS, list(SPORTS_MARKETS["Soccer"].values())),
]
MAJOR_PROP_MARKETS = {
    sport_key: table
    for table, sport_keys in _PROP_TABLE_LEAGUES
    for sport_key in sport_keys
}

# ── Game-level markets for the QueryList panel ────────────────────────────────
# Each entry is an ordered dict of {SECTION_NAME: [market_key, ...]}. The
# QueryList panel renders one header per non-empty section, so section names are
# free-form (GAME LINES / QUARTERS / HALVES / PERIODS / INNINGS / SETS / SECONDARY).
#
# Market-key naming follows the CURRENT Odds API docs:
#   https://the-odds-api.com/sports-odds-data/betting-markets.html
# The legacy "_1st_quarter / _1st_half / _1st_period" keys this file used before
# are REJECTED by the live API (HTTP 422 INVALID_MARKET, verified 2026-06-09).
# Period/quarter/half/inning markets are "additional markets" — they only return
# data from the per-event endpoint /sports/{sport}/events/{id}/odds, not bulk /odds.
#
# Period suffixes:  quarters q1-q4, halves h1-h2, hockey periods p1-p3,
#                   baseball innings 1st_1/1st_3/1st_5/1st_7_innings, tennis sets s1-s2.

# Documented full set of period market bases, in docs order. Quarters/halves/
# periods support team totals; innings do not.
_PERIOD_BASES = ["h2h", "h2h_3_way", "spreads", "alternate_spreads",
                 "totals", "alternate_totals", "team_totals", "alternate_team_totals"]
_INNING_BASES = ["h2h", "h2h_3_way", "spreads", "alternate_spreads",
                 "totals", "alternate_totals"]

def _period_markets(suffixes, bases=_PERIOD_BASES):
    """Expand period suffixes into every documented market key, grouped by period."""
    return [f"{base}_{suf}" for suf in suffixes for base in bases]

_FOOTBALL_GAME = {
    "GAME LINES": ["h2h", "spreads", "totals", "alternate_spreads",
                   "alternate_totals", "team_totals", "alternate_team_totals"],
    "QUARTERS":   _period_markets(["q1", "q2", "q3", "q4"]),
    "HALVES":     _period_markets(["h1", "h2"]),
    "SECONDARY":  [],
}
_NBA_GAME = {
    "GAME LINES": ["h2h", "spreads", "totals", "alternate_spreads",
                   "alternate_totals", "team_totals", "alternate_team_totals"],
    "QUARTERS":   _period_markets(["q1", "q2", "q3", "q4"]),
    "HALVES":     _period_markets(["h1", "h2"]),
    "SECONDARY":  [],
}
_MLB_GAME = {
    "GAME LINES": ["h2h", "spreads", "alternate_spreads", "totals",
                   "alternate_totals", "team_totals", "alternate_team_totals"],
    "INNINGS":    _period_markets(["1st_1_innings", "1st_3_innings",
                                   "1st_5_innings", "1st_7_innings"], _INNING_BASES),
    "SECONDARY":  [],
}
_NHL_GAME = {
    "GAME LINES": ["h2h", "h2h_3_way", "spreads", "alternate_spreads", "totals",
                   "alternate_totals", "team_totals", "alternate_team_totals"],
    "PERIODS":    _period_markets(["p1", "p2", "p3"]),
    "SECONDARY":  [],
}
_SOCCER_GAME = {
    "GAME LINES": ["h2h", "h2h_3_way", "draw_no_bet", "double_chance", "btts",
                   "spreads", "alternate_spreads", "totals", "alternate_totals",
                   "team_totals", "alternate_team_totals"],
    "SECONDARY":  ["alternate_spreads_corners", "alternate_totals_corners",
                   "alternate_spreads_cards", "alternate_totals_cards"],
}
_TENNIS_GAME = {
    "GAME LINES": ["h2h", "spreads", "totals"],
    "SETS":       ["h2h_s1", "h2h_s2", "spreads_s1", "totals_s1", "alternate_totals_s1"],
    "SECONDARY":  [],
}
_BASIC_GAME   = {"GAME LINES": ["h2h"], "SECONDARY": []}
_CRICKET_GAME = {"GAME LINES": ["h2h", "spreads", "totals"], "SECONDARY": []}

# Back-compat aliases (older code referenced _NFL_GAME)
_NFL_GAME = _FOOTBALL_GAME

GAME_MARKETS = {
    "americanfootball_nfl":              _NFL_GAME,
    "americanfootball_nfl_preseason":    _NFL_GAME,
    "americanfootball_ncaaf":            _NFL_GAME,
    "americanfootball_cfl":              _NFL_GAME,
    "americanfootball_ufl":              _NFL_GAME,
    "baseball_mlb":                      _MLB_GAME,
    "baseball_mlb_preseason":            _MLB_GAME,
    "baseball_milb":                     _MLB_GAME,
    "baseball_npb":                      _MLB_GAME,
    "baseball_kbo":                      _MLB_GAME,
    "baseball_ncaa":                     _MLB_GAME,
    "basketball_nba":                    _NBA_GAME,
    "basketball_wnba":                   _NBA_GAME,
    "basketball_ncaab":                  _NBA_GAME,
    "basketball_wncaab":                 _NBA_GAME,
    "basketball_euroleague":             _NBA_GAME,
    "basketball_nbl":                    _NBA_GAME,
    "icehockey_nhl":                     _NHL_GAME,
    "icehockey_sweden_hockey_league":    _NHL_GAME,
    "icehockey_sweden_allsvenskan":      _NHL_GAME,
    "soccer_epl":                        _SOCCER_GAME,
    "soccer_usa_mls":                    _SOCCER_GAME,
    "soccer_germany_bundesliga":         _SOCCER_GAME,
    "soccer_germany_bundesliga2":        _SOCCER_GAME,
    "soccer_spain_la_liga":              _SOCCER_GAME,
    "soccer_spain_segunda_division":     _SOCCER_GAME,
    "soccer_italy_serie_a":              _SOCCER_GAME,
    "soccer_italy_serie_b":              _SOCCER_GAME,
    "soccer_france_ligue_one":           _SOCCER_GAME,
    "soccer_france_ligue_two":           _SOCCER_GAME,
    "soccer_uefa_champs_league":         _SOCCER_GAME,
    "soccer_uefa_europa_league":         _SOCCER_GAME,
    "soccer_uefa_europa_conference_league": _SOCCER_GAME,
    "soccer_conmebol_copa_libertadores": _SOCCER_GAME,
    "soccer_conmebol_copa_america":      _SOCCER_GAME,
    "soccer_mexico_ligamx":              _SOCCER_GAME,
    "soccer_argentina_primera_division": _SOCCER_GAME,
    "soccer_brazil_campeonato":          _SOCCER_GAME,
    "soccer_netherlands_eredivisie":     _SOCCER_GAME,
    "soccer_portugal_primeira_liga":     _SOCCER_GAME,
    "soccer_turkey_super_league":        _SOCCER_GAME,
    "soccer_spl":                        _SOCCER_GAME,
    "soccer_efl_champ":                  _SOCCER_GAME,
    "soccer_england_league1":            _SOCCER_GAME,
    "soccer_england_league2":            _SOCCER_GAME,
    "tennis_atp_wimbledon":              _TENNIS_GAME,
    "tennis_atp_us_open":                _TENNIS_GAME,
    "tennis_atp_french_open":            _TENNIS_GAME,
    "tennis_atp_aus_open_singles":       _TENNIS_GAME,
    "tennis_atp_canadian_open":          _TENNIS_GAME,
    "tennis_atp_cincinnati_open":        _TENNIS_GAME,
    "tennis_wta_wimbledon":              _TENNIS_GAME,
    "tennis_wta_us_open":                _TENNIS_GAME,
    "tennis_wta_french_open":            _TENNIS_GAME,
    "tennis_wta_aus_open_singles":       _TENNIS_GAME,
    "tennis_wta_canadian_open":          _TENNIS_GAME,
    "tennis_wta_cincinnati_open":        _TENNIS_GAME,
    "mma_mixed_martial_arts":            _BASIC_GAME,
    "boxing_boxing":                     _BASIC_GAME,
    "rugbyleague_nrl":                   _CRICKET_GAME,
    "lacrosse_pll":                      _CRICKET_GAME,
    "cricket_ipl":                       _CRICKET_GAME,
    "cricket_big_bash":                  _CRICKET_GAME,
    "cricket_test_match":                _CRICKET_GAME,
    "cricket_caribbean_premier_league":  _CRICKET_GAME,
    # ── Leagues added from the live /v4/sports listing ──
    "basketball_nba_preseason":          _NBA_GAME,
    "basketball_nba_summer_league":      _NBA_GAME,
    "basketball_nba_all_stars":          _NBA_GAME,
    "icehockey_ahl":                     _NHL_GAME,
    "icehockey_liiga":                   _NHL_GAME,
    "icehockey_mestis":                  _NHL_GAME,
    "icehockey_nhl_preseason":           _NHL_GAME,
    "cricket_asia_cup":                  _CRICKET_GAME,
    "cricket_icc_trophy":                _CRICKET_GAME,
    "cricket_icc_world_cup_womens":      _CRICKET_GAME,
    "cricket_t20_world_cup":             _CRICKET_GAME,
    "cricket_the_hundred":               _CRICKET_GAME,
    "lacrosse_ncaa":                     _CRICKET_GAME,
    "rugbyleague_nrl_state_of_origin":   _CRICKET_GAME,
    "rugbyunion_six_nations":            _CRICKET_GAME,
    "handball_germany_bundesliga":       _CRICKET_GAME,
    "soccer_saudi_arabia_pro_league":    _SOCCER_GAME,
    "soccer_russia_premier_league":      _SOCCER_GAME,
    "soccer_germany_dfb_pokal":          _SOCCER_GAME,
    "soccer_italy_coppa_italia":         _SOCCER_GAME,
    "soccer_spain_copa_del_rey":         _SOCCER_GAME,
    "soccer_france_coupe_de_france":     _SOCCER_GAME,
    "soccer_concacaf_leagues_cup":       _SOCCER_GAME,
    "soccer_concacaf_gold_cup":          _SOCCER_GAME,
    "soccer_conmebol_copa_sudamericana": _SOCCER_GAME,
    "soccer_fifa_club_world_cup":        _SOCCER_GAME,
    "soccer_fifa_world_cup":             _SOCCER_GAME,
    "soccer_fifa_world_cup_womens":      _SOCCER_GAME,
    "soccer_fifa_world_cup_qualifiers_europe":         _SOCCER_GAME,
    "soccer_fifa_world_cup_qualifiers_south_america":  _SOCCER_GAME,
    "soccer_germany_bundesliga_women":   _SOCCER_GAME,
    "soccer_uefa_champs_league_women":   _SOCCER_GAME,
    "tennis_atp_indian_wells":           _TENNIS_GAME,
    "tennis_atp_miami_open":             _TENNIS_GAME,
    "tennis_atp_monte_carlo_masters":    _TENNIS_GAME,
    "tennis_atp_madrid_open":            _TENNIS_GAME,
    "tennis_atp_italian_open":           _TENNIS_GAME,
    "tennis_atp_barcelona_open":         _TENNIS_GAME,
    "tennis_atp_hamburg_open":           _TENNIS_GAME,
    "tennis_atp_munich":                 _TENNIS_GAME,
    "tennis_atp_dubai":                  _TENNIS_GAME,
    "tennis_atp_qatar_open":             _TENNIS_GAME,
    "tennis_wta_indian_wells":           _TENNIS_GAME,
    "tennis_wta_miami_open":             _TENNIS_GAME,
    "tennis_wta_madrid_open":            _TENNIS_GAME,
    "tennis_wta_italian_open":           _TENNIS_GAME,
    "tennis_wta_charleston_open":        _TENNIS_GAME,
    "tennis_wta_stuttgart_open":         _TENNIS_GAME,
    "tennis_wta_dubai":                  _TENNIS_GAME,
    "tennis_wta_qatar_open":             _TENNIS_GAME,
    "tennis_wta_strasbourg":             _TENNIS_GAME,
    "tennis_wta_queens_club_champ":      _TENNIS_GAME,
}

# ── QueryList market labels ───────────────────────────────────────────────────
# Short abbreviations shown on the QueryList checkboxes / markets-button summary.
# Generated from each key so the full documented period set stays in sync.

# Base market → abbreviation
_BASE_LABELS = {
    "h2h":                   "ML",
    "h2h_3_way":             "3W",
    "spreads":               "SPR",
    "alternate_spreads":     "ALT-SPR",
    "totals":                "TOT",
    "alternate_totals":      "ALT-TOT",
    "team_totals":           "TM-TOT",
    "alternate_team_totals": "ALT-TM-TOT",
    "draw_no_bet":           "DNB",
    "btts":                  "BTTS",
    "double_chance":         "DC",
}
# Period suffix → prefix abbreviation
_PERIOD_LABELS = {
    "q1": "1Q", "q2": "2Q", "q3": "3Q", "q4": "4Q",
    "h1": "1H", "h2": "2H",
    "p1": "P1", "p2": "P2", "p3": "P3",
    "s1": "S1", "s2": "S2",
    "1st_1_innings": "I1", "1st_3_innings": "I3",
    "1st_5_innings": "F5", "1st_7_innings": "I7",
}
# Non-period markets that don't decompose into base+suffix
_MANUAL_LABELS = {
    "alternate_spreads_corners": "CRN-H",
    "alternate_totals_corners":  "CRN-T",
    "alternate_spreads_cards":   "CRD-H",
    "alternate_totals_cards":    "CRD-T",
}

def _label_for(key):
    if key in _MANUAL_LABELS:
        return _MANUAL_LABELS[key]
    # longest suffix first so "1st_5_innings" wins over any shorter match
    for suf in sorted(_PERIOD_LABELS, key=len, reverse=True):
        if key.endswith("_" + suf):
            base = key[: -(len(suf) + 1)]
            return f"{_PERIOD_LABELS[suf]}-{_BASE_LABELS.get(base, base)}"
    return _BASE_LABELS.get(key, key)

# Build the label map from every key referenced by GAME_MARKETS.
GAME_MARKET_LABELS = {
    key: _label_for(key)
    for mmap in GAME_MARKETS.values()
    for keys in mmap.values()
    for key in keys
}
