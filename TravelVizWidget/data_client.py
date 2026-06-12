import concurrent
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import requests
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any, Set
from dataclasses import dataclass
from PyQt6.QtCore import QObject, pyqtSignal, QThread
from bs4 import BeautifulSoup
import re
import math
import logging
import time
import threading
from pathlib import Path
import amadeus
from database_manager import DatabaseManager, GameStatus, TeamInfo, Venue, GameData, TeamTravelData

logger = logging.getLogger(__name__)
# from amadeus import analytics, airline, airport

# Update mapping for LA kings (schedule url uses la not lak)
#TODO: Add in relavant flight/airport data from amadeus API
# Endpoints to consider: Airport Routes, Airport Nearest Relevant, Airport & City Search, Hotel List, 
# Hotel Search, Hotel Ratings, Points of Interest, Location Score, Flight Delay Prediction,
# Airport On Time Performance,On Demand Flight Status,Flight Busiest Traveling Period

# Teams that own their own planes: Detroit Tigers, Los Angeles Lakers, Dallas Mavericks, 
# Boston Celtics, Houston Rockets, Golden State Warriors, and Cleveland Cavaliers
# Try out openskynetwork API for live flight data and by plane filtering to find team charter flights


@dataclass
class ForbesHotel:
    """Forbes hotel data from database"""
    hotel_name: str
    destination: str
    star_rating: str
    
    def get_numeric_rating(self) -> int:
        """Convert Forbes rating to numeric scale"""
        numeric_star_rating = {
            "Z-NOT_RATED": 0,
            "SOON_TO_BE_RATED": 2,
            "RECOMMENDED": 3,
            "FOUR_STAR": 4,
            "FIVE_STAR": 5,
        }
        return numeric_star_rating.get(self.star_rating, 0)

@dataclass
class HotelOption:
    """Hotel near venue with quality metrics"""
    hotel_id: str
    name: str
    distance_from_venue: float  # km
    overall_rating: int  # 0-100
    price_tier: str  # "LUXURY", "UPSCALE", "MIDSCALE"
    amenities: List[str]
    location_score: int  # 0-100
    coordinates: Tuple[float, float]
    forbes_rating: Optional[ForbesHotel] = None

@dataclass
class AirportInfo:
    """Airport information with performance metrics"""
    iata_code: str
    name: str
    city: str
    distance_from_venue: float  # km
    on_time_probability: float  # 0-1
    traveler_score: int
    coordinates: Tuple[float, float]
    timezone_offset: str

@dataclass
class RouteInsights:
    """Travel intelligence between two cities"""
    game_data: 'GameData'
    primary_airport: AirportInfo
    alternate_airports: List[AirportInfo]
    destination_hotels: List[HotelOption]
    travel_distance: float  # miles
    travel_confidence: str  # "HIGH", "MEDIUM", "LOW"
    risk_factors: List[str]
    travel_data: Optional['TeamTravelData'] = None

@dataclass
class TeamTravelIntelligence:
    """Complete travel intelligence for a team's upcoming schedule"""
    team_info: 'TeamInfo'
    upcoming_routes: List[RouteInsights]
    total_travel_distance: float
    highest_risk_route: Optional[RouteInsights]
    travel_complexity_score: float  # 0-100
    recommendations: List[str]
    analysis_timestamp: datetime





class AmadeusAnalyzer:
    """Enhanced Amadeus analyzer with Forbes hotel integration and geolocation-based search"""
    
    # Configuration constants
    MIN_REQUEST_INTERVAL = 0.1
    MAX_WORKERS = 6
    CONFIDENCE_THRESHOLD = 0.75
    HOTEL_RADIUS_KM = 25
    STRICT_RADIUS_KM = 20
    MIN_HOTEL_RATING = 75
    
    # Hotel brand mappings
    HOTEL_BRANDS = {
        'four seasons': ['four seasons', 'fs '],
        'ritz carlton': ['ritz carlton', 'ritz-carlton', 'ritzcarlton'],
        'peninsula': ['peninsula'], 'park hyatt': ['park hyatt'], 'hyatt': ['hyatt'],
        'waldorf astoria': ['waldorf astoria', 'waldorf-astoria'], 'langham': ['langham'],
        'mandarin oriental': ['mandarin oriental', 'mandarin'], 'st regis': ['st regis', 'st. regis'],
        'conrad': ['conrad'], 'w hotel': [' w ', 'w hotel'], 'beverly hills hotel': ['beverly hills hotel'],
        'edition': ['edition'], 'trump': ['trump'], 'fairmont': ['fairmont'],
        'intercontinental': ['intercontinental'], 'marriott': ['marriott'], 'hilton': ['hilton']
    }
    
    # Location keywords for matching
    LOCATION_KEYWORDS = {
        'beverly hills', 'hollywood', 'downtown', 'airport', 'central', 'park',
        'manhattan', 'times square', 'midtown', 'santa monica', 'west hollywood'
    }
    
    def __init__(self, api_key: str, api_secret: str, aggregator=None):
        import amadeus
        
        self.amadeus = amadeus.Client(client_id=api_key, client_secret=api_secret)
        self.aggregator = aggregator
        self.logger = logging.getLogger(__name__)
        
        # Caches
        self._airport_cache: Dict[str, List[AirportInfo]] = {}
        self._hotel_cache: Dict[str, List[HotelOption]] = {}
        self._performance_cache: Dict[str, float] = {}
        self._failed_matches_cache: Set[str] = set()
        
        # Rate limiting
        self.last_request_time = 0
        self.request_lock = threading.Lock()
        
        # Thread pool for concurrent processing
        self.executor = ThreadPoolExecutor(max_workers=self.MAX_WORKERS)
        
        # Performance flags
        self.skip_performance_calls = False
    
    def analyze_team_travel(self, team_info: 'TeamInfo', upcoming_games: List['GameData'], days_ahead: int = 14) -> TeamTravelIntelligence:
        """Main analysis method with concurrent processing"""
        cutoff_date = datetime.now() + timedelta(days=days_ahead)
        
        relevant_games = [
            game for game in upcoming_games 
            if game.date <= cutoff_date and (
                game.home_team.team_id == team_info.team_id or 
                game.away_team.team_id == team_info.team_id
            )
        ]
        relevant_games.sort(key=lambda x: x.date)
        
        # Find venue changes
        routes_to_analyze = []
        for i in range(len(relevant_games) - 1):
            current_game, next_game = relevant_games[i], relevant_games[i + 1]
            if current_game.venue.venue_id != next_game.venue.venue_id:
                routes_to_analyze.append((current_game.venue, next_game.venue, next_game))
        
        if not routes_to_analyze:
            return self._create_empty_intelligence(team_info)
        
        route_insights = self._analyze_routes_concurrently(routes_to_analyze)
        total_distance = sum(route.travel_distance for route in route_insights)
        
        return TeamTravelIntelligence(
            team_info=team_info,
            upcoming_routes=route_insights,
            total_travel_distance=total_distance,
            highest_risk_route=self._identify_highest_risk_route(route_insights),
            travel_complexity_score=self._calculate_complexity_score(route_insights),
            recommendations=self._generate_recommendations(route_insights),
            analysis_timestamp=datetime.now()
        )
    
    def _analyze_routes_concurrently(self, routes: List[Tuple]) -> List[RouteInsights]:
        """Analyze multiple routes concurrently"""
        route_insights = []
        future_to_route = {
            self.executor.submit(self._analyze_route_between_venues, *route): route[0].city + " → " + route[1].city
            for route in routes
        }
        
        for future in concurrent.futures.as_completed(future_to_route):
            route_name = future_to_route[future]
            try:
                result = future.result(timeout=30)
                if result:
                    route_insights.append(result)
            except Exception as e:
                logger.warning(f"⚠️ Route failed: {route_name} - {e}")
        
        return route_insights
    
    def _analyze_route_between_venues(self, origin_venue: Venue, destination_venue: Venue, game: GameData) -> RouteInsights:
        """Analyze a specific travel route"""
        destination_airports = self._get_venue_airports(destination_venue, game.date)
        hotels = self._get_destination_hotels_by_geolocation(destination_venue, game.date)
        
        return RouteInsights(
            game_data=game,
            primary_airport=destination_airports[0] if destination_airports else None,
            alternate_airports=destination_airports[1:3] if len(destination_airports) > 1 else [],
            destination_hotels=hotels[:5],
            travel_distance=self._calculate_distance(origin_venue.city, destination_venue.city),
            travel_confidence=self._assess_travel_confidence(destination_airports[0] if destination_airports else None, hotels),
            risk_factors=self._identify_risk_factors(destination_airports[0] if destination_airports else None, game.date)
        )
    
    def _get_destination_hotels_by_geolocation(self, venue: Venue, check_in_date: datetime) -> List[HotelOption]:
        """Get hotels using pure geolocation approach"""
        cache_key = f"geo_{venue.latitude}_{venue.longitude}"
        if cache_key in self._hotel_cache:
            return self._hotel_cache[cache_key]
        
        # Get Amadeus hotels near stadium
        amadeus_hotels = self._get_amadeus_hotels_by_coordinates(venue)
        if not amadeus_hotels:
            return []
        
        # Get Forbes hotels and match them
        forbes_hotels = self._get_forbes_hotels(venue.city)
        if forbes_hotels:
            matched_hotels = self._match_forbes_to_amadeus(forbes_hotels, amadeus_hotels, venue)
            # Fill remaining slots with quality Amadeus hotels
            if len(matched_hotels) < 8:
                matched_hotels.extend(self._get_additional_quality_hotels(matched_hotels, amadeus_hotels, venue))
        else:
            matched_hotels = self._create_hotels_from_amadeus_only(amadeus_hotels, venue)
        
        # Sort by Forbes rating, then distance
        matched_hotels.sort(key=lambda h: (
            -(h.forbes_rating.get_numeric_rating() if h.forbes_rating else 3),
            h.distance_from_venue
        ))
        
        self._hotel_cache[cache_key] = matched_hotels
        
        # Concise debug output
        logger.debug(f"🏨 {venue.city}: {len(amadeus_hotels)} Amadeus → {len(forbes_hotels)} Forbes → {len(matched_hotels)} final hotels")
        logger.debug(f"matched_hotels: {matched_hotels}")
        return matched_hotels
    
    def _get_amadeus_hotels_by_coordinates(self, venue: Venue) -> List[Dict]:
        """Get Amadeus hotels using coordinate search"""
        try:
            self._wait_for_rate_limit()
            response = self.amadeus.reference_data.locations.hotels.by_geocode.get(
                latitude=venue.latitude, longitude=venue.longitude,
                radius=self.HOTEL_RADIUS_KM, radiusUnit='KM'
            )
            return response.data if response.data else []
        except Exception as e:
            logger.error(f"❌ Amadeus API error for {venue.city}: {e}")
            return []
    
    def _match_forbes_to_amadeus(self, forbes_hotels: List[ForbesHotel], amadeus_hotels: List[Dict], venue: Venue) -> List[HotelOption]:
        """Match Forbes hotels to Amadeus hotels"""
        matched, matched_ids = [], set()
        
        for forbes_hotel in forbes_hotels:
            cache_key = f"{forbes_hotel.hotel_name}_{venue.city}"
            if cache_key in self._failed_matches_cache:
                continue
            
            best_match, confidence = self._find_best_amadeus_match(forbes_hotel, amadeus_hotels)
            logger.debug(f'{best_match}')
            if (best_match and best_match['hotelId'] not in matched_ids and confidence >= self.CONFIDENCE_THRESHOLD):
                distance = self._calculate_hotel_distance(best_match, venue)
                if distance <= self.HOTEL_RADIUS_KM:
                    matched.append(self._create_hotel_option(best_match, forbes_hotel, distance))
                    matched_ids.add(best_match['hotelId'])
                else:
                    self._failed_matches_cache.add(cache_key)
            else:
                self._failed_matches_cache.add(cache_key)
        
        return matched
    
    def _find_best_amadeus_match(self, forbes_hotel: ForbesHotel, amadeus_hotels: List[Dict]) -> Tuple[Optional[Dict], float]:
        """Find best matching Amadeus hotel with confidence scoring"""
        if not amadeus_hotels:
            return None, 0.0
        
        forbes_name = forbes_hotel.hotel_name.lower()
        best_match, best_confidence = None, 0.0
        
        for amadeus_hotel in amadeus_hotels:
            amadeus_name = amadeus_hotel.get('name', '').lower()
            
            # Calculate different similarity scores
            brand_score = self._calculate_brand_similarity(forbes_name, amadeus_name)
            name_score = self._calculate_name_similarity(forbes_name, amadeus_name)
            location_score = self._calculate_location_similarity(forbes_name, amadeus_name)
            
            # Determine final confidence with strategy prioritization
            if brand_score > 0.8:
                confidence = brand_score
            elif name_score > 0.7:
                confidence = name_score
            elif location_score > 0.8:
                confidence = location_score * 0.7
            elif brand_score > 0.5 and name_score > 0.3:
                confidence = (brand_score * 0.7) + (name_score * 0.3)
            else:
                confidence = max(brand_score, name_score, location_score) * 0.5
            
            if confidence > best_confidence:
                best_confidence = confidence
                best_match = amadeus_hotel
        
        return best_match, best_confidence
    
    def _calculate_brand_similarity(self, forbes_name: str, amadeus_name: str) -> float:
        """Calculate brand similarity score"""
        # Find Forbes brand
        forbes_brand = None
        for brand, variations in self.HOTEL_BRANDS.items():
            if any(var.strip() in forbes_name for var in variations if len(var.strip()) > 2):
                forbes_brand = brand
                break
        
        if not forbes_brand:
            return 0.0
        
        # Check Amadeus name for same brand
        brand_variations = self.HOTEL_BRANDS[forbes_brand]
        if any(var.strip() in amadeus_name for var in brand_variations if len(var.strip()) > 2):
            return 1.0
        
        # Check partial brand matches
        brand_words = [w for w in forbes_brand.split() if len(w) > 3]
        amadeus_words = amadeus_name.split()
        
        matches = sum(1 for brand_word in brand_words 
                     for amadeus_word in amadeus_words
                     if brand_word == amadeus_word or 
                        (len(brand_word) > 4 and brand_word in amadeus_word))
        
        return matches / len(brand_words) if brand_words else 0.0
    
    def _calculate_name_similarity(self, forbes_name: str, amadeus_name: str) -> float:
        """Calculate name similarity score"""
        def clean_name(name):
            # Remove spa terms and common words
            for term in ['spa at', 'spa', 'chuan spa', 'health club']:
                name = name.replace(term, ' ')
            
            words = [w.strip('.,();') for w in name.split() 
                    if w not in {'hotel', 'resort', 'suites', 'suite', 'inn', 'lodge', 
                                'the', 'a', 'an', 'and', 'at', 'by', 'of', 'in', '&'} 
                    and len(w) > 2 and not w.isdigit()]
            return ' '.join(words)
        
        clean_forbes = clean_name(forbes_name)
        clean_amadeus = clean_name(amadeus_name)
        
        if not clean_forbes or not clean_amadeus:
            return 0.0
        
        # Exact matches
        if clean_forbes == clean_amadeus:
            return 1.0
        elif clean_forbes in clean_amadeus or clean_amadeus in clean_forbes:
            return 0.9
        
        # Word overlap
        forbes_words = set(clean_forbes.split())
        amadeus_words = set(clean_amadeus.split())
        
        if forbes_words and amadeus_words:
            intersection = forbes_words.intersection(amadeus_words)
            if len(intersection) >= 2:
                return len(intersection) / len(forbes_words.union(amadeus_words))
            elif len(intersection) == 1:
                matching_word = list(intersection)[0]
                return 0.6 if len(matching_word) > 4 else 0.0
        
        return 0.0
    
    def _calculate_location_similarity(self, forbes_name: str, amadeus_name: str) -> float:
        """Calculate location-based similarity score"""
        forbes_locations = [loc for loc in self.LOCATION_KEYWORDS if loc in forbes_name]
        amadeus_locations = [loc for loc in self.LOCATION_KEYWORDS if loc in amadeus_name]
        
        if forbes_locations and amadeus_locations:
            for floc in forbes_locations:
                for aloc in amadeus_locations:
                    if floc == aloc:
                        return 1.0
                    elif floc in aloc or aloc in floc:
                        return 0.8
        return 0.0
    
    def _create_hotel_option(self, amadeus_hotel: Dict, forbes_hotel: ForbesHotel, distance: float) -> HotelOption:
        """Create HotelOption from matched hotels"""
        return HotelOption(
            hotel_id=amadeus_hotel['hotelId'],
            name=amadeus_hotel.get('name', forbes_hotel.hotel_name),
            distance_from_venue=distance,
            overall_rating=forbes_hotel.get_numeric_rating() * 20,
            price_tier="LUXURY" if forbes_hotel.get_numeric_rating() >= 4 else "UPSCALE",
            amenities=self._get_amenities_for_forbes_hotel(forbes_hotel),
            location_score=max(0, 100 - int(distance * 4)),
            coordinates=(amadeus_hotel['geoCode']['latitude'], amadeus_hotel['geoCode']['longitude']),
            forbes_rating=forbes_hotel
        )
    
    def _get_additional_quality_hotels(self, existing_hotels: List[HotelOption], amadeus_hotels: List[Dict], venue: Venue) -> List[HotelOption]:
        """Get additional quality hotels from Amadeus"""
        existing_ids = {h.hotel_id for h in existing_hotels}
        additional = []
        
        for hotel_data in amadeus_hotels:
            if (hotel_data['hotelId'] not in existing_ids and len(additional) < 5):
                distance = self._calculate_hotel_distance(hotel_data, venue)
                if distance <= self.STRICT_RADIUS_KM and self._estimate_hotel_rating(hotel_data) >= self.MIN_HOTEL_RATING:
                    additional.append(self._create_amadeus_hotel_option(hotel_data, distance))
        
        return additional
    
    def _create_hotels_from_amadeus_only(self, amadeus_hotels: List[Dict], venue: Venue) -> List[HotelOption]:
        """Create hotel options from Amadeus data only"""
        hotels = []
        for hotel_data in amadeus_hotels:
            distance = self._calculate_hotel_distance(hotel_data, venue)
            if distance <= self.HOTEL_RADIUS_KM:
                hotels.append(self._create_amadeus_hotel_option(hotel_data, distance))
        
        hotels.sort(key=lambda h: (h.distance_from_venue, -h.overall_rating))
        return hotels
    
    def _create_amadeus_hotel_option(self, hotel_data: Dict, distance: float) -> HotelOption:
        """Create HotelOption from Amadeus data only"""
        return HotelOption(
            hotel_id=hotel_data['hotelId'],
            name=hotel_data['name'],
            distance_from_venue=distance,
            overall_rating=self._estimate_hotel_rating(hotel_data),
            price_tier=self._determine_price_tier(hotel_data),
            amenities=self._extract_amenities(hotel_data),
            location_score=max(0, 100 - int(distance * 4)),
            coordinates=(hotel_data['geoCode']['latitude'], hotel_data['geoCode']['longitude']),
            forbes_rating=None
        )
    
    def _get_forbes_hotels(self, city_name: str) -> List[ForbesHotel]:
        """Get Forbes hotels from database"""
        if not self.aggregator or not hasattr(self.aggregator, 'db'):
            return []
        
        # City variations for search
        cities_to_search = [city_name]
        city_variations = {
            "Los Angeles": ["Beverly Hills", "Hollywood", "Santa Monica"],
            "New York": ["Manhattan", "Brooklyn"],
            "Washington": ["Washington DC"],
            "Miami": ["Miami Beach", "South Beach"]
        }
        cities_to_search.extend(city_variations.get(city_name, []))
        
        try:
            with sqlite3.connect(self.aggregator.db.db_path) as conn:
                conn.row_factory = sqlite3.Row
                placeholders = ','.join(['?' for _ in cities_to_search])
                
                cursor = conn.execute(f"""
                    SELECT hotel_name, destination, star_rating 
                    FROM hotels 
                    WHERE destination IN ({placeholders})
                    AND star_rating IN ('FIVE_STAR', 'FOUR_STAR', 'RECOMMENDED')
                    ORDER BY CASE star_rating WHEN 'FIVE_STAR' THEN 5 WHEN 'FOUR_STAR' THEN 4 ELSE 3 END DESC
                    LIMIT 20
                """, cities_to_search)
                
                return [ForbesHotel(
                    hotel_name=self._clean_forbes_hotel_name(row['hotel_name']),
                    destination=row['destination'],
                    star_rating=row['star_rating']
                ) for row in cursor.fetchall()]
        except Exception:
            return []
    
    def _clean_forbes_hotel_name(self, name: str) -> str:
        """Clean Forbes hotel names for better matching"""
        cleaned = re.sub(r'\b(spa at|chuan spa|spa)\b', '', name, flags=re.IGNORECASE).strip()
        return ' '.join(cleaned.split())
    
    # Utility methods
    def _wait_for_rate_limit(self):
        """Ensure API rate limit compliance"""
        with self.request_lock:
            elapsed = time.time() - self.last_request_time
            if elapsed < self.MIN_REQUEST_INTERVAL:
                time.sleep(self.MIN_REQUEST_INTERVAL - elapsed)
            self.last_request_time = time.time()
    
    def _calculate_hotel_distance(self, hotel_data: Dict, venue: Venue) -> float:
        """Calculate distance between hotel and venue"""
        if ('geoCode' not in hotel_data):
            logger.debug(f"could not calculate distance: {hotel_data}")
            return 999999
        geo = hotel_data.get('geoCode', {})
        
        return self._haversine_distance(
                venue.latitude, venue.longitude,
                float(geo.get('latitude', 0)), float(geo.get('longitude', 0))
            )
    
    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between coordinates in km"""
        R = 6371  # Earth's radius in km
        dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        a = (math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * 
             math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    def _calculate_distance(self, origin_city: str, destination_city: str) -> float:
        """Calculate distance between cities"""
        try:
            city_coords = (self.aggregator.espn_scraper.city_coordinates 
                          if self.aggregator and hasattr(self.aggregator, 'espn_scraper') 
                          else {})
            
            origin_coords = city_coords.get(origin_city)
            dest_coords = city_coords.get(destination_city)
            
            if origin_coords and dest_coords:
                return self._haversine_distance(*origin_coords, *dest_coords) * 0.621371
            return 800.0
        except Exception:
            return 800.0
    
    def _estimate_hotel_rating(self, hotel_data: dict) -> int:
        """Estimate hotel rating from available data"""
        name, chain = hotel_data.get('name', '').upper(), hotel_data.get('chainCode', '').upper()
        
        luxury_indicators = (['RITZ', 'FOUR SEASONS', 'MANDARIN', 'ST. REGIS', 'WALDORF'], 
                            {'RT', 'RC', 'LX', 'BU', 'SL', 'WR', 'PH', 'FS', 'JW'})
        upscale_indicators = (['HILTON', 'MARRIOTT', 'HYATT', 'WESTIN', 'SHERATON'],
                             {'HI', 'HL', 'DT', 'MP', 'HW', 'IC', 'IN', 'AC'})
        
        if chain in luxury_indicators[1] or any(kw in name for kw in luxury_indicators[0]):
            return 90
        elif chain in upscale_indicators[1] or any(kw in name for kw in upscale_indicators[0]):
            return 80
        return 70
    
    def _determine_price_tier(self, hotel_data: dict) -> str:
        """Determine hotel price tier"""
        rating = self._estimate_hotel_rating(hotel_data)
        return "LUXURY" if rating >= 90 else "UPSCALE" if rating >= 80 else "MIDSCALE"
    
    def _extract_amenities(self, hotel_data: dict) -> List[str]:
        """Extract amenities from hotel data"""
        name = hotel_data.get('name', '').upper()
        amenities = ['WIFI', 'PARKING']
        
        if any(kw in name for kw in ['RESORT', 'SPA']):
            amenities.append('SPA')
        if any(kw in name for kw in ['SUITES', 'SUITE']):
            amenities.append('SUITES')
        if 'AIRPORT' in name:
            amenities.append('AIRPORT SHUTTLE')
        
        return amenities[:6]
    
    def _get_amenities_for_forbes_hotel(self, forbes_hotel: ForbesHotel) -> List[str]:
        """Get amenities based on Forbes rating"""
        rating = forbes_hotel.get_numeric_rating()
        amenities = ['WIFI']
        
        if rating >= 3:
            amenities.extend(['CONCIERGE', 'BUSINESS CENTER'])
        if rating >= 4:
            amenities.extend(['SPA', 'FITNESS CENTER', 'FINE DINING', 'VALET PARKING'])
        if rating >= 5:
            amenities.extend(['BUTLER SERVICE', 'LUXURY AMENITIES'])
        
        return amenities
    
    # Airport and risk assessment methods
    def _get_venue_airports(self, venue: Venue, travel_date: datetime) -> List[AirportInfo]:
        """Get airports near venue with caching"""
        cache_key = f"{venue.city}_{venue.latitude}_{venue.longitude}"
        
        if cache_key not in self._airport_cache:
            self._airport_cache[cache_key] = self._fetch_airports_for_venue(venue)
        
        airports = self._airport_cache[cache_key]
        
        # Update performance data if needed
        if not self.skip_performance_calls and airports:
            primary_airport = airports[0]
            perf_key = f"{primary_airport.iata_code}_{travel_date.strftime('%Y-%m-%d')}"
            if perf_key not in self._performance_cache:
                self._performance_cache[perf_key] = self._get_airport_performance(primary_airport.iata_code, travel_date)
            primary_airport.on_time_probability = self._performance_cache[perf_key]
        
        return sorted(airports, key=lambda x: x.distance_from_venue)
    
    def _fetch_airports_for_venue(self, venue: Venue) -> List[AirportInfo]:
        """Fetch airports from Amadeus API"""
        try:
            self._wait_for_rate_limit()
            response = self.amadeus.reference_data.locations.airports.get(
                latitude=venue.latitude, longitude=venue.longitude, radius=100
            )
            
            airports = []
            for airport_data in response.data:
                distance = self._haversine_distance(
                    venue.latitude, venue.longitude,
                    airport_data['geoCode']['latitude'], 
                    airport_data['geoCode']['longitude']
                )
                
                airports.append(AirportInfo(
                    iata_code=airport_data['iataCode'],
                    name=airport_data['name'],
                    city=airport_data['address']['cityName'],
                    distance_from_venue=distance,
                    on_time_probability=0.85,
                    traveler_score=airport_data.get('analytics', {}).get('travelers', {}).get('score', 75),
                    coordinates=(airport_data['geoCode']['latitude'], airport_data['geoCode']['longitude']),
                    timezone_offset=airport_data.get('timeZoneOffset', '')
                ))
            
            return airports
        except Exception as e:
            self.logger.error(f"Failed to fetch airports for {venue.city}: {e}")
            return []
    
    def _get_airport_performance(self, airport_code: str, date: datetime) -> float:
        """Get airport on-time performance"""
        try:
            self._wait_for_rate_limit()
            response = self.amadeus.airport.predictions.on_time.get(
                airportCode=airport_code, date=date.strftime('%Y-%m-%d')
            )
            return float(response.data['probability'])
        except:
            return 0.85
    
    def _assess_travel_confidence(self, airport: AirportInfo, hotels: List[HotelOption]) -> str:
        """Assess overall travel confidence"""
        if not airport or not hotels:
            return "LOW"
        
        airport_score = airport.on_time_probability
        hotel_score = max(h.overall_rating for h in hotels) / 100 if hotels else 0
        combined_score = (airport_score + hotel_score) / 2
        
        return "HIGH" if combined_score > 0.8 else "MEDIUM" if combined_score > 0.6 else "LOW"
    
    def _identify_risk_factors(self, airport: AirportInfo, date: datetime) -> List[str]:
        """Identify travel risk factors"""
        risks = []
        
        if airport and airport.on_time_probability < 0.7:
            risks.append("Poor airport on-time performance")
        
        if airport:
            month = date.month
            if month in [12, 1, 2] and airport.iata_code in ['DEN', 'MSP', 'BOS']:
                risks.append("Winter weather concerns")
            elif month in [6, 7, 8] and airport.iata_code in ['ORD', 'DFW', 'ATL', 'MIA']:
                risks.append("Summer thunderstorm season")
        
        return risks
    
    # Intelligence generation methods
    def _create_empty_intelligence(self, team_info: 'TeamInfo') -> TeamTravelIntelligence:
        """Create empty intelligence for teams with no travel"""
        return TeamTravelIntelligence(
            team_info=team_info, upcoming_routes=[], total_travel_distance=0,
            highest_risk_route=None, travel_complexity_score=0,
            recommendations=["No upcoming travel required"], analysis_timestamp=datetime.now()
        )
    
    def _identify_highest_risk_route(self, routes: List[RouteInsights]) -> Optional[RouteInsights]:
        """Find highest risk route"""
        return min(routes, key=lambda r: r.primary_airport.on_time_probability if r.primary_airport else 0) if routes else None
    
    def _calculate_complexity_score(self, routes: List[RouteInsights]) -> float:
        """Calculate travel complexity score (0-100)"""
        if not routes:
            return 0
        
        total_distance = sum(r.travel_distance for r in routes)
        avg_performance = sum(r.primary_airport.on_time_probability for r in routes if r.primary_airport) / len(routes)
        
        distance_score = min(total_distance / 100, 100)
        performance_score = (1 - avg_performance) * 100
        
        return (distance_score + performance_score) / 2
    
    def _generate_recommendations(self, routes: List[RouteInsights]) -> List[str]:
        """Generate travel recommendations"""
        high_risk_routes = [r for r in routes if r.travel_confidence == "LOW"]
        return ([f"Monitor {len(high_risk_routes)} high-risk routes closely"] if high_risk_routes 
                else ["All routes look good for upcoming travel"])


class AmadeusWorker(QThread):
    """Simplified Amadeus worker that uses existing travel_data table"""
    progressUpdated = pyqtSignal(int, str)  # percentage, message
    intelligenceReady = pyqtSignal(object)
    errorOccurred = pyqtSignal(str)
    
    def __init__(self, aggregator, team_abbr: str, days_ahead: int):
        super().__init__()
        self.aggregator = aggregator
        self.team_abbr = team_abbr
        self.days_ahead = days_ahead
        self.cancelled = False
    
    def cancel(self):
        """Cancel the analysis"""
        self.cancelled = True
    
    def run(self):
        """Simplified run method using travel_data table"""
        try:
            logger.debug(f"🔍 AmadeusWorker starting analysis for team {self.team_abbr}")
            
            if self.cancelled:
                return
            
            self.progressUpdated.emit(10, "Loading team information...")
            
            # Get team info
            team_info = self.aggregator.get_team_info(self.team_abbr)
            if not team_info:
                self.errorOccurred.emit(f"Team {self.team_abbr} not found")
                return
            
            self.progressUpdated.emit(20, "Finding upcoming travel...")
            
            # Get upcoming travel data from database
            from datetime import datetime, timedelta
            now = datetime.now()
            cutoff = now + timedelta(days=self.days_ahead)
            
            season = self.aggregator.current_season or "2025"
            league = self.aggregator.current_league
            
            # Get all travel for this team, then filter by date
            all_travel = self.aggregator.db.load_travel_data(season, league, self.team_abbr)
            
            upcoming_travel = [
                travel for travel in all_travel
                if travel.travel_date and now <= travel.travel_date <= cutoff
            ]
            
            logger.debug(f"📊 Found {len(upcoming_travel)} upcoming travel segments")
            
            if self.cancelled:
                return
            
            if not upcoming_travel:
                # No upcoming travel - create empty intelligence
                intelligence = TeamTravelIntelligence(
                    team_info=team_info,
                    upcoming_routes=[],
                    total_travel_distance=0,
                    highest_risk_route=None,
                    travel_complexity_score=0,
                    recommendations=["No upcoming travel required"],
                    analysis_timestamp=datetime.now()
                )
                self.intelligenceReady.emit(intelligence)
                return
            
            self.progressUpdated.emit(40, f"Analyzing {len(upcoming_travel)} routes...")
            
            # Process each travel record with Amadeus
            route_insights = []
            total_distance = 0
            
            for i, travel in enumerate(upcoming_travel):
                if self.cancelled:
                    return
                
                progress = 40 + int((i / len(upcoming_travel)) * 50)  # 40% to 90%
                self.progressUpdated.emit(progress, f"Analyzing route to {travel.arrival_city}...")
                
                try:
                    # Create mock venue objects for Amadeus analysis
                    origin_venue = self._create_venue_from_city(travel.departure_city)
                    dest_venue = self._create_venue_from_city(travel.arrival_city)
                    
                    # Create mock game object
                    mock_game = self._create_mock_game(travel, team_info)
                    
                    # Use Amadeus to analyze this route
                    if hasattr(self.aggregator, 'amadeus_analyzer') and self.aggregator.amadeus_analyzer:
                        route = self.aggregator.amadeus_analyzer._analyze_route_between_venues(
                            origin_venue, dest_venue, mock_game
                        )
                        route.travel_data = travel  # Attach original travel data
                        route_insights.append(route)
                        total_distance += route.travel_distance
                        
                        logger.debug(f"📊 Analyzed: {travel.departure_city} → {travel.arrival_city}")
                    
                except Exception as e:
                    logger.warning(f"⚠️ Route analysis failed for {travel.departure_city} → {travel.arrival_city}: {e}")
                    continue
            
            self.progressUpdated.emit(95, "Compiling intelligence...")
            
            # Build final intelligence
            intelligence = TeamTravelIntelligence(
                team_info=team_info,
                upcoming_routes=route_insights,
                total_travel_distance=total_distance,
                highest_risk_route=self._find_highest_risk_route(route_insights),
                travel_complexity_score=self._calculate_complexity_score(route_insights),
                recommendations=self._generate_recommendations(route_insights),
                analysis_timestamp=datetime.now()
            )
            
            self.progressUpdated.emit(100, "Analysis complete!")
            
            if not self.cancelled:
                self.intelligenceReady.emit(intelligence)
                logger.info(f"✅ Amadeus analysis complete for {self.team_abbr}")
            
        except Exception as e:
            if not self.cancelled:
                error_msg = f"Amadeus analysis failed: {str(e)}"
                logger.error(f"❌ {error_msg}")
                self.errorOccurred.emit(error_msg)
    
    def _create_venue_from_city(self, city_name: str):
        """Create a venue object from city name using existing coordinates"""
        coords = self.aggregator.espn_scraper.city_coordinates.get(city_name, (0.0, 0.0))
        
        return Venue(
            venue_id=f"{city_name}_venue",
            name=f"{city_name} Venue",
            city=city_name,
            state="",
            country="USA",
            latitude=coords[0],
            longitude=coords[1]
        )
    
    def _create_mock_game(self, travel, team_info):
        """Create a mock game object for Amadeus analysis"""
        
        # Create opponent team info
        opponent_team = TeamInfo(
            team_id="opponent",
            abbreviation="OPP",
            display_name=travel.opponent or "Opponent",
            location=travel.arrival_city,
            color="#000000",
            alternate_color="#FFFFFF"
        )
        
        # Create venue
        venue = self._create_venue_from_city(travel.arrival_city)
        
        return GameData(
            game_id=travel.game_id or f"mock_{travel.travel_date.strftime('%Y%m%d')}",
            date=travel.game_date,
            home_team=opponent_team,  # Opponent is home team (we're traveling to them)
            away_team=team_info,      # Our team is away team  
            venue=venue,
            status=GameStatus.SCHEDULED,
            league=self.aggregator.current_league,
            season=self.aggregator.current_season
        )
    
    def _find_highest_risk_route(self, routes):
        """Find route with highest risk"""
        if not routes:
            return None
        return min(routes, key=lambda r: r.primary_airport.on_time_probability if r.primary_airport else 0)
    
    def _calculate_complexity_score(self, routes):
        """Calculate complexity score"""
        if not routes:
            return 0
        
        total_distance = sum(r.travel_distance for r in routes)
        avg_performance = sum(r.primary_airport.on_time_probability for r in routes if r.primary_airport) / len(routes) if routes else 0.85
        
        distance_score = min(total_distance / 100, 100)
        performance_score = (1 - avg_performance) * 100
        
        return (distance_score + performance_score) / 2
    
    def _generate_recommendations(self, routes):
        """Generate recommendations"""
        recommendations = []
        
        high_risk_routes = [r for r in routes if r.travel_confidence == "LOW"]
        if high_risk_routes:
            recommendations.append(f"Monitor {len(high_risk_routes)} high-risk routes")
        
        if not recommendations:
            recommendations.append("All routes look good")
        
        return recommendations


class ESPNScheduleScraper:
    """Scraper for ESPN team schedule pages supporting MLB, NBA, and NHL with proper MLB half handling"""
    
    def __init__(self):
        # League-specific URL patterns - MLB requires half parameter, others don't
        self.url_patterns = {
            'MLB': "https://www.espn.com/mlb/team/schedule/_/name/{team}/seasontype/2/half/{half}",
            'NBA': "https://www.espn.com/nba/team/schedule/_/name/{team}/season/{year}/seasontype/2",
            'NHL': "https://www.espn.com/nhl/team/schedule/_/name/{team}/season/{year}/seasontype/2"
        }
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Initialize league-specific data
        self.team_airports = self.load_team_airports()
        self.league_teams = self.get_all_league_teams()
        self.city_coordinates = self.load_city_coordinates()
    
    def get_all_league_teams(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Get all teams for all supported leagues"""
        return {
            'MLB': self.get_mlb_teams(),
            'NBA': self.get_nba_teams(),
            'NHL': self.get_nhl_teams()
        }
    
    def get_mlb_teams(self) -> Dict[str, Dict[str, str]]:
        """Get MLB team abbreviations and info for ESPN URLs"""
        return {
            # American League East
            "bal": {"name": "Baltimore Orioles", "city": "Baltimore", "division": "AL East", "conference": "American League"},
            "bos": {"name": "Boston Red Sox", "city": "Boston", "division": "AL East", "conference": "American League"},
            "nyy": {"name": "New York Yankees", "city": "New York", "division": "AL East", "conference": "American League"},
            "tb": {"name": "Tampa Bay Rays", "city": "Tampa", "division": "AL East", "conference": "American League"},
            "tor": {"name": "Toronto Blue Jays", "city": "Toronto", "division": "AL East", "conference": "American League"},
            
            # American League Central
            # White Sox are chw not cws
            "chw": {"name": "Chicago White Sox", "city": "Chicago", "division": "AL Central", "conference": "American League"},
            "cle": {"name": "Cleveland Guardians", "city": "Cleveland", "division": "AL Central", "conference": "American League"},
            "det": {"name": "Detroit Tigers", "city": "Detroit", "division": "AL Central", "conference": "American League"},
            "kc": {"name": "Kansas City Royals", "city": "Kansas City", "division": "AL Central", "conference": "American League"},
            "min": {"name": "Minnesota Twins", "city": "Minneapolis", "division": "AL Central", "conference": "American League"},
            
            # American League West
            # Athletics go by ath not oak
            "hou": {"name": "Houston Astros", "city": "Houston", "division": "AL West", "conference": "American League"},
            "laa": {"name": "Los Angeles Angels", "city": "Los Angeles", "division": "AL West", "conference": "American League"},
            "ath": {"name": "Athletics", "city": "Sacramento", "division": "AL West", "conference": "American League"},
            "sea": {"name": "Seattle Mariners", "city": "Seattle", "division": "AL West", "conference": "American League"},
            "tex": {"name": "Texas Rangers", "city": "Dallas", "division": "AL West", "conference": "American League"},
            
            # National League East
            "atl": {"name": "Atlanta Braves", "city": "Atlanta", "division": "NL East", "conference": "National League"},
            "mia": {"name": "Miami Marlins", "city": "Miami", "division": "NL East", "conference": "National League"},
            "nym": {"name": "New York Mets", "city": "New York", "division": "NL East", "conference": "National League"},
            "phi": {"name": "Philadelphia Phillies", "city": "Philadelphia", "division": "NL East", "conference": "National League"},
            "wsh": {"name": "Washington Nationals", "city": "Washington", "division": "NL East", "conference": "National League"},
            
            # National League Central
            "chc": {"name": "Chicago Cubs", "city": "Chicago", "division": "NL Central", "conference": "National League"},
            "cin": {"name": "Cincinnati Reds", "city": "Cincinnati", "division": "NL Central", "conference": "National League"},
            "mil": {"name": "Milwaukee Brewers", "city": "Milwaukee", "division": "NL Central", "conference": "National League"},
            "pit": {"name": "Pittsburgh Pirates", "city": "Pittsburgh", "division": "NL Central", "conference": "National League"},
            "stl": {"name": "St. Louis Cardinals", "city": "St. Louis", "division": "NL Central", "conference": "National League"},
            
            # National League West
            "ari": {"name": "Arizona Diamondbacks", "city": "Phoenix", "division": "NL West", "conference": "National League"},
            "col": {"name": "Colorado Rockies", "city": "Denver", "division": "NL West", "conference": "National League"},
            "lad": {"name": "Los Angeles Dodgers", "city": "Los Angeles", "division": "NL West", "conference": "National League"},
            "sd": {"name": "San Diego Padres", "city": "San Diego", "division": "NL West", "conference": "National League"},
            "sf": {"name": "San Francisco Giants", "city": "San Francisco", "division": "NL West", "conference": "National League"},
        }
    
    def get_nba_teams(self) -> Dict[str, Dict[str, str]]:
        """Get NBA team abbreviations and info for ESPN URLs"""
        return {
            # Eastern Conference - Atlantic Division
            "bos": {"name": "Boston Celtics", "city": "Boston", "division": "Atlantic", "conference": "Eastern"},
            "bkn": {"name": "Brooklyn Nets", "city": "New York", "division": "Atlantic", "conference": "Eastern"},
            "ny": {"name": "New York Knicks", "city": "New York", "division": "Atlantic", "conference": "Eastern"},
            "phi": {"name": "Philadelphia 76ers", "city": "Philadelphia", "division": "Atlantic", "conference": "Eastern"},
            "tor": {"name": "Toronto Raptors", "city": "Toronto", "division": "Atlantic", "conference": "Eastern"},
            
            # Eastern Conference - Central Division
            "chi": {"name": "Chicago Bulls", "city": "Chicago", "division": "Central", "conference": "Eastern"},
            "cle": {"name": "Cleveland Cavaliers", "city": "Cleveland", "division": "Central", "conference": "Eastern"},
            "det": {"name": "Detroit Pistons", "city": "Detroit", "division": "Central", "conference": "Eastern"},
            "ind": {"name": "Indiana Pacers", "city": "Indianapolis", "division": "Central", "conference": "Eastern"},
            "mil": {"name": "Milwaukee Bucks", "city": "Milwaukee", "division": "Central", "conference": "Eastern"},
            
            # Eastern Conference - Southeast Division
            "atl": {"name": "Atlanta Hawks", "city": "Atlanta", "division": "Southeast", "conference": "Eastern"},
            "cha": {"name": "Charlotte Hornets", "city": "Charlotte", "division": "Southeast", "conference": "Eastern"},
            "mia": {"name": "Miami Heat", "city": "Miami", "division": "Southeast", "conference": "Eastern"},
            "orl": {"name": "Orlando Magic", "city": "Orlando", "division": "Southeast", "conference": "Eastern"},
            "wsh": {"name": "Washington Wizards", "city": "Washington", "division": "Southeast", "conference": "Eastern"},
            
            # Western Conference - Northwest Division
            "den": {"name": "Denver Nuggets", "city": "Denver", "division": "Northwest", "conference": "Western"},
            "min": {"name": "Minnesota Timberwolves", "city": "Minneapolis", "division": "Northwest", "conference": "Western"},
            "okc": {"name": "Oklahoma City Thunder", "city": "Oklahoma City", "division": "Northwest", "conference": "Western"},
            "por": {"name": "Portland Trail Blazers", "city": "Portland", "division": "Northwest", "conference": "Western"},
            "utah": {"name": "Utah Jazz", "city": "Salt Lake City", "division": "Northwest", "conference": "Western"},
            
            # Western Conference - Pacific Division
            "gs": {"name": "Golden State Warriors", "city": "San Francisco", "division": "Pacific", "conference": "Western"},
            "lac": {"name": "LA Clippers", "city": "Los Angeles", "division": "Pacific", "conference": "Western"},
            "lal": {"name": "Los Angeles Lakers", "city": "Los Angeles", "division": "Pacific", "conference": "Western"},
            "phx": {"name": "Phoenix Suns", "city": "Phoenix", "division": "Pacific", "conference": "Western"},
            "sac": {"name": "Sacramento Kings", "city": "Sacramento", "division": "Pacific", "conference": "Western"},
            
            # Western Conference - Southwest Division
            "dal": {"name": "Dallas Mavericks", "city": "Dallas", "division": "Southwest", "conference": "Western"},
            "hou": {"name": "Houston Rockets", "city": "Houston", "division": "Southwest", "conference": "Western"},
            "mem": {"name": "Memphis Grizzlies", "city": "Memphis", "division": "Southwest", "conference": "Western"},
            "no": {"name": "New Orleans Pelicans", "city": "New Orleans", "division": "Southwest", "conference": "Western"},
            "sa": {"name": "San Antonio Spurs", "city": "San Antonio", "division": "Southwest", "conference": "Western"},
        }
    
    def get_nhl_teams(self) -> Dict[str, Dict[str, str]]:
        """Get NHL team abbreviations and info for ESPN URLs"""
        return {
            # Eastern Conference - Atlantic Division
            "bos": {"name": "Boston Bruins", "city": "Boston", "division": "Atlantic", "conference": "Eastern"},
            "buf": {"name": "Buffalo Sabres", "city": "Buffalo", "division": "Atlantic", "conference": "Eastern"},
            "det": {"name": "Detroit Red Wings", "city": "Detroit", "division": "Atlantic", "conference": "Eastern"},
            "fla": {"name": "Florida Panthers", "city": "Sunrise", "division": "Atlantic", "conference": "Eastern"},
            "mtl": {"name": "Montreal Canadiens", "city": "Montreal", "division": "Atlantic", "conference": "Eastern"},
            "ott": {"name": "Ottawa Senators", "city": "Ottawa", "division": "Atlantic", "conference": "Eastern"},
            "tb": {"name": "Tampa Bay Lightning", "city": "Tampa", "division": "Atlantic", "conference": "Eastern"},
            "tor": {"name": "Toronto Maple Leafs", "city": "Toronto", "division": "Atlantic", "conference": "Eastern"},
            
            # Eastern Conference - Metropolitan Division
            "car": {"name": "Carolina Hurricanes", "city": "Raleigh", "division": "Metropolitan", "conference": "Eastern"},
            "cbj": {"name": "Columbus Blue Jackets", "city": "Columbus", "division": "Metropolitan", "conference": "Eastern"},
            "njd": {"name": "New Jersey Devils", "city": "New Jersey", "division": "Metropolitan", "conference": "Eastern"},
            "nyi": {"name": "New York Islanders", "city": "New York", "division": "Metropolitan", "conference": "Eastern"},
            "nyr": {"name": "New York Rangers", "city": "New York", "division": "Metropolitan", "conference": "Eastern"},
            "phi": {"name": "Philadelphia Flyers", "city": "Philadelphia", "division": "Metropolitan", "conference": "Eastern"},
            "pit": {"name": "Pittsburgh Penguins", "city": "Pittsburgh", "division": "Metropolitan", "conference": "Eastern"},
            "wsh": {"name": "Washington Capitals", "city": "Washington", "division": "Metropolitan", "conference": "Eastern"},
            
            # Western Conference - Central Division
            "utah": {"name": "Utah Hockey Club", "city": "Utah", "division": "Central", "conference": "Western"},
            "chi": {"name": "Chicago Blackhawks", "city": "Chicago", "division": "Central", "conference": "Western"},
            "col": {"name": "Colorado Avalanche", "city": "Denver", "division": "Central", "conference": "Western"},
            "dal": {"name": "Dallas Stars", "city": "Dallas", "division": "Central", "conference": "Western"},
            "min": {"name": "Minnesota Wild", "city": "Minneapolis", "division": "Central", "conference": "Western"},
            "nsh": {"name": "Nashville Predators", "city": "Nashville", "division": "Central", "conference": "Western"},
            "stl": {"name": "St. Louis Blues", "city": "St. Louis", "division": "Central", "conference": "Western"},
            "wpg": {"name": "Winnipeg Jets", "city": "Winnipeg", "division": "Central", "conference": "Western"},
            
            # Western Conference - Pacific Division
            "ana": {"name": "Anaheim Ducks", "city": "Anaheim", "division": "Pacific", "conference": "Western"},
            "cgy": {"name": "Calgary Flames", "city": "Calgary", "division": "Pacific", "conference": "Western"},
            "edm": {"name": "Edmonton Oilers", "city": "Edmonton", "division": "Pacific", "conference": "Western"},
            "la": {"name": "Los Angeles Kings", "city": "Los Angeles", "division": "Pacific", "conference": "Western"},
            "sj": {"name": "San Jose Sharks", "city": "San Jose", "division": "Pacific", "conference": "Western"},
            "sea": {"name": "Seattle Kraken", "city": "Seattle", "division": "Pacific", "conference": "Western"},
            "van": {"name": "Vancouver Canucks", "city": "Vancouver", "division": "Pacific", "conference": "Western"},
            "vgk": {"name": "Vegas Golden Knights", "city": "Las Vegas", "division": "Pacific", "conference": "Western"},
        }
    
    def load_team_airports(self) -> Dict[str, str]:
        """Load mapping of team cities to airport codes for all leagues"""
        return {
            # Major US Cities
            "New York": "LGA", "Los Angeles": "LAX", "Chicago": "ORD", "San Francisco": "SFO",
            "Boston": "BOS", "Philadelphia": "PHL", "Atlanta": "ATL", "Houston": "IAH",
            "Miami": "MIA", "Washington": "DCA", "St. Louis": "STL", "Milwaukee": "MKE",
            "Denver": "DEN", "Phoenix": "PHX", "San Diego": "SAN", "Baltimore": "BWI",
            "Tampa": "TPA", "Cleveland": "CLE", "Detroit": "DTW", "Minneapolis": "MSP",
            "Kansas City": "MCI", "Seattle": "SEA", "Oakland": "OAK", "Dallas": "DFW",
            "Cincinnati": "CVG", "Pittsburgh": "PIT",
            
            # NBA-specific cities
            "Indianapolis": "IND", "Charlotte": "CLT", "Orlando": "MCO", "Portland": "PDX",
            "Sacramento": "SMF", "Salt Lake City": "SLC", "Oklahoma City": "OKC",
            "Memphis": "MEM", "New Orleans": "MSY", "San Antonio": "SAT",
            
            # NHL-specific cities
            "Buffalo": "BUF", "Sunrise": "FLL", "Raleigh": "RDU", "Columbus": "CMH",
            "Newark": "EWR", "Nashville": "BNA", "Anaheim": "SNA", "Las Vegas": "LAS",
            "San Jose": "SJC",
            
            # Canadian cities
            "Toronto": "YYZ", "Montreal": "YUL", "Vancouver": "YVR", "Calgary": "YYC",
            "Edmonton": "YEG", "Ottawa": "YOW", "Winnipeg": "YWG"
        }
    
    def load_city_coordinates(self) -> Dict[str, Tuple[float, float]]:
        """City coordinates for MLB, NHL, NBA"""
        return {
            
            "Phoenix": (33.4484, -112.0740), "Atlanta": (33.7490, -84.3880),
            "Baltimore": (39.2904, -76.6122), "Boston": (42.3601, -71.0589),
            "Chicago": (41.8781, -87.6298), "Cincinnati": (39.1031, -84.5120),
            "Cleveland": (41.4993, -81.6944), "Denver": (39.7392, -104.9903),
            "Detroit": (42.3314, -83.0458), "Houston": (29.7604, -95.3698),
            "Kansas City": (39.0997, -94.5786), "Los Angeles": (34.0522, -118.2437),
            "Miami": (25.7617, -80.1918), "Milwaukee": (43.0389, -87.9065),
            "Minneapolis": (44.9778, -93.2650), "New York": (40.7128, -74.0060),
            "Oakland": (37.8044, -122.2712), "Philadelphia": (39.9526, -75.1652),
            "Pittsburgh": (40.4406, -79.9959), "San Diego": (32.7157, -117.1611),
            "San Francisco": (37.7749, -122.4194), "Seattle": (47.6062, -122.3321),
            "St. Louis": (38.6270, -90.1994), "Tampa": (27.9506, -82.4572),
            "Dallas": (32.7767, -96.7970), "Washington": (38.9072, -77.0369),
            
            
            "Indianapolis": (39.7684, -86.1581), "Charlotte": (35.2271, -80.8431),
            "Orlando": (28.5383, -81.3792), "Portland": (45.5152, -122.6784),
            "Sacramento": (38.5816, -121.4944), "Salt Lake City": (40.7608, -111.8910),
            "Oklahoma City": (35.4676, -97.5164), "Memphis": (35.1495, -90.0490),
            "New Orleans": (29.9511, -90.0715), "San Antonio": (29.4241, -98.4936),
            
            
            "Buffalo": (42.8864, -78.8784), "Sunrise": (26.1354, -80.2373),
            "Raleigh": (35.7796, -78.6382), "Columbus": (39.9612, -82.9988),
            "Newark": (40.7357, -74.1724), "Nashville": (36.1627, -86.7816),
            "Anaheim": (33.8366, -117.9143), "Las Vegas": (36.1699, -115.1398),
            "San Jose": (37.3382, -121.8863),
            
            # Canadian cities
            "Toronto": (43.6532, -79.3832), "Montreal": (45.5017, -73.5673),
            "Vancouver": (49.2827, -123.1207), "Calgary": (51.0447, -114.0719),
            "Edmonton": (53.5461, -113.4938), "Ottawa": (45.4215, -75.6972),
            "Winnipeg": (49.8951, -97.1384)
        }
    
    def format_season_for_league(self, year: int, league: str) -> str:
        """Format season string based on league conventions"""
        if league in ['NBA', 'NHL']:
            next_year = str(year + 1)[2:]  # Get last 2 digits
            return f"{year}-{next_year}"   # e.g., "2024-25"
        else:  # MLB
            return str(year)               # e.g., "2024"
    
    def get_current_season_for_league(self, league: str) -> str:
        """Get current season string for a league based on current date"""
        now = datetime.now()
        current_year = now.year
        
        if league in ['NBA', 'NHL']:
            # NBA/NHL seasons start in October and end in June of next year
            if now.month >= 10:  # October or later
                return self.format_season_for_league(current_year, league)
            else:  # Before October
                return self.format_season_for_league(current_year - 1, league)
        else:  # MLB
            # MLB season is calendar year
            return self.format_season_for_league(current_year, league)
    
    def scrape_team_schedule(self, team_abbrev: str, league: str, season: str = None) -> List[GameData]:
        """Scrape season schedule for a team in specified league with proper MLB half handling"""
        if season is None:
            season = self.get_current_season_for_league(league)
        
        all_games = []
        
        if league == 'MLB':
            # MLB SPECIAL HANDLING: ESPN divides MLB season into two halves
            logger.debug(f"Scraping MLB {team_abbrev} schedule for {season} season (both halves)...")
            
            for half in [1, 2]:
                try:
                    url = self.url_patterns['MLB'].format(team=team_abbrev, half=half)
                    logger.debug(f"  → Scraping MLB {team_abbrev} half {half}: {url}")
                    
                    response = self.session.get(url, timeout=15)
                    response.raise_for_status()
                    
                    table_rows = self._parse_schedule_page(response.text, team_abbrev, league, season)
                    
                    if table_rows and len(table_rows) > 1:
                        half_games = self.parse_table_to_games(table_rows, team_abbrev, league, season)
                        all_games.extend(half_games)
                        logger.debug(f"  ✓ Half {half}: Found {len(half_games)} games")
                    else:
                        logger.debug(f"  ✗ Half {half}: No schedule data found")
                    
                    time.sleep(0.25)
                    
                except requests.exceptions.RequestException as e:
                    logger.debug(f"  ✗ Network error scraping MLB {team_abbrev} half {half}: {e}")
                    continue
                except Exception as e:
                    logger.debug(f"  ✗ Error parsing MLB {team_abbrev} half {half}: {e}")
                    continue
            
            logger.debug(f"MLB {team_abbrev} total games scraped: {len(all_games)}")
        
        else:  # NBA or NHL - Single schedule page
            try:
                # Determine if we should use season-specific URL or current season URL
                current_season = self.get_current_season_for_league(league)

                if season == current_season:
                    # Use simple URL for current season (ESPN default)
                    url = f"https://www.espn.com/{league.lower()}/team/schedule/_/name/{team_abbrev}/seasontype/2"
                    logger.debug(f"Scraping {league} {team_abbrev} current season ({season}): {url}")
                else:
                    # Use season-specific URL for historical data
                    year = season.split('-')[0] if '-' in season else season
                    url = self.url_patterns[league].format(team=team_abbrev, year=year)
                    logger.debug(f"Scraping {league} {team_abbrev} historical season ({season}): {url}")

                response = self.session.get(url, timeout=15)
                response.raise_for_status()

                table_rows = self._parse_schedule_page(response.text, team_abbrev, league, season)

                if table_rows and len(table_rows) > 1:
                    games = self.parse_table_to_games(table_rows, team_abbrev, league, season)
                    all_games.extend(games)
                    logger.debug(f"  ✓ Found {len(games)} {league} games")
                else:
                    logger.debug(f"  ✗ No {league} schedule data found")

                time.sleep(0.75)

            except requests.exceptions.RequestException as e:
                logger.debug(f"  ✗ Network error scraping {league} {team_abbrev}: {e}")
            except Exception as e:
                logger.debug(f"  ✗ Error parsing {league} {team_abbrev}: {e}")
        
        return all_games
    
    def scrape_league_schedule(self, league: str, season: str = None) -> List[GameData]:
        """Scrape schedules for all teams in a specific league"""
        if league not in self.league_teams:
            raise ValueError(f"Unsupported league: {league}. Supported: {list(self.league_teams.keys())}")
        
        if season is None:
            season = self.get_current_season_for_league(league)
        
        logger.debug(f"\n=== Scraping {league} {season} League Schedule ===")
        
        all_games = []
        seen_games = set()
        league_teams = self.league_teams[league]
        total_teams = len(league_teams)
        
        for team_idx, (team_abbrev, team_info) in enumerate(league_teams.items(), 1):
            try:
                logger.debug(f"\n[{team_idx}/{total_teams}] Scraping {league} {team_info['name']} ({team_abbrev})...")
                
                team_games = self.scrape_team_schedule(team_abbrev, league, season)
                
                games_added = 0
                for game in team_games:
                    game_key = f"{game.date.strftime('%Y-%m-%d')}_{game.home_team.abbreviation}_{game.away_team.abbreviation}"
                    
                    if game_key not in seen_games:
                        all_games.append(game)
                        seen_games.add(game_key)
                        games_added += 1
                
                logger.debug(f"  → Added {games_added} unique games (found {len(team_games)} total)")
                
                if team_idx < total_teams:
                    time.sleep(1.5)
                
            except Exception as e:
                logger.debug(f"  ✗ Error scraping {league} team {team_abbrev}: {e}")
                continue
        
        logger.debug(f"\n=== {league} {season} Scraping Complete ===")
        logger.debug(f"Total unique games scraped: {len(all_games)}")
        
        return all_games
    
    def _parse_schedule_page(self, html_content: str, team_abbrev: str, league: str, season: str) -> List[List[str]]:
        """Parse ESPN schedule page HTML to extract table data - FIXED href parsing logic"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            tables = soup.find_all('table')
            
            schedule_table = None
            for table in tables:
                rows = table.find_all('tr')
                if len(rows) > 5:
                    first_few_rows_text = ' '.join([row.get_text().lower() for row in rows[:3]])
                    schedule_keywords = ['date', 'opponent', 'result', 'vs', '@', 'matchup', 'game']
                    
                    if any(keyword in first_few_rows_text for keyword in schedule_keywords):
                        schedule_table = table
                        break
            
            if not schedule_table:
                all_rows = soup.find_all('tr')
                if len(all_rows) > 5:
                    rows = all_rows
                else:
                    logger.debug(f"  ✗ No schedule data found for {league} {team_abbrev}")
                    return []
            else:
                rows = schedule_table.find_all('tr')
            
            table_data = []
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if cells:
                    cell_texts = []
                    for cell in cells:
                        # 🔧 FIXED: Proper team name extraction from ESPN href
                        team_link = cell.find('a', href=True)
                        if team_link and 'team/_/name/' in team_link['href']:
                            # Extract team abbrev from href like "/mlb/team/_/name/laa/los-angeles-angels"
                            href_parts = team_link['href'].split('/')
                            if len(href_parts) >= 6 and href_parts[4] == 'name':
                                team_abbrev_from_href = href_parts[5].upper()
                                cell_text = cell.get_text(strip=True)
                                
                                # 🔧 FIXED: Proper replacement logic for ambiguous cities
                                # Instead of replacing just the last word, replace the entire team name
                                
                                if any(ambiguous in cell_text.lower() for ambiguous in ['new york', 'los angeles', 'chicago']):
                                    # Extract the vs/@ prefix if present
                                    prefix = ""
                                    team_part = cell_text
                                    
                                    if cell_text.startswith('vs'):
                                        prefix = "vs"
                                        team_part = cell_text[2:].strip()
                                    elif cell_text.startswith('@'):
                                        prefix = "@"
                                        team_part = cell_text[1:].strip()
                                    
                                    # Replace the entire team name with just the abbreviation
                                    if prefix:
                                        cell_text = prefix + team_abbrev_from_href
                                    else:
                                        cell_text = team_abbrev_from_href
                                
                                cell_texts.append(cell_text)
                            else:
                                cell_texts.append(cell.get_text(strip=True))
                        else:
                            cell_texts.append(cell.get_text(strip=True))
                    
                    if cell_texts and len(cell_texts) >= 3:
                        table_data.append(cell_texts)
            
            logger.debug(f"  → Extracted {len(table_data)} rows from schedule page")
            return table_data
            
        except Exception as e:
            logger.debug(f"  ✗ Error parsing schedule page for {league} {team_abbrev}: {e}")
            return []
    
    def parse_table_to_games(self, table_rows: List[List[str]], team_abbrev: str, league: str, season: str) -> List[GameData]:
        """Convert table rows to GameData objects"""
        games = []
        
        if not table_rows or len(table_rows) < 2:
            return games
        
        logger.debug(f"  → Processing {len(table_rows)-1} {league} game rows for {team_abbrev}...")
        
        valid_games = 0
        rejected_games = 0
        
        for i, row in enumerate(table_rows[1:], 1):
            try:
                game = self.parse_game_row(row, team_abbrev, league, season)
                if game:
                    games.append(game)
                    valid_games += 1
                    if valid_games <= 3:
                        logger.debug(f"    ✓ Game {valid_games}: {game.away_team.abbreviation} @ {game.home_team.abbreviation} on {game.date.strftime('%m/%d')}")
                    elif valid_games == 4:
                        logger.debug(f"    ... processing remaining games ...")
                else:
                    rejected_games += 1
                        
            except Exception as e:
                rejected_games += 1
                continue
        
        logger.debug(f"  → Successfully parsed {valid_games} valid {league} games for {team_abbrev}")
        if rejected_games > 0:
            logger.warning(f"  ⚠️  Rejected {rejected_games} rows")
                
        return games
    
    def parse_game_row(self, row: List[str], team_abbrev: str, league: str, season: str) -> Optional[GameData]:
        """Parse individual game row into GameData - simplified with debugging and filtering"""
        if len(row) < 3:
            return None
            
        date_str = row[0].strip()
        opponent_str = row[1].strip()
        result_str = row[2].strip() if len(row) > 2 else ""
        
        # ✅ Skip header rows
        if date_str.upper() in ['DATE', 'DAY'] or opponent_str.upper() in ['OPPONENT', 'OPP']:
            return None
        
        # ✅ Skip postponed games  
        if 'postponed' in result_str.lower():
            if league == 'MLB':
                logger.debug(f"    📅 Skipped postponed: '{opponent_str}'")
            return None
        
        # ✅ Skip live games - they're already happening
        if 'live' in result_str.lower() or 'live' in date_str.lower():
            if league == 'MLB':
                logger.debug(f"    ⏰ Skipped live game: '{opponent_str}'")
            return None
        
        if not date_str or not opponent_str:
            return None
        
        # ✅ DEBUG: Show what we're trying to parse
        if league in ['NHL', 'MLB']:
            logger.debug(f"    🔍 Parsing: '{date_str}' | '{opponent_str}' | '{result_str}'")
        
        # Parse game date
        try:
            game_date = self.parse_date_for_league(date_str, league, season)
            if not game_date:
                if league in ['NHL', 'MLB']:
                    logger.error(f"    ❌ Date parse failed: '{date_str}'")
                return None
        except Exception as e:
            if league in ['NHL', 'MLB']:
                logger.error(f"    ❌ Date parse exception: '{date_str}' - {e}")
            return None
            
        # Parse opponent
        try:
            is_home, opponent_abbrev = self.parse_opponent(opponent_str, league)
            if not opponent_abbrev:
                if league in ['NHL', 'MLB']:
                    logger.error(f"    ❌ Opponent parse failed: '{opponent_str}'")
                return None
        except Exception as e:
            if league in ['NHL', 'MLB']:
                logger.error(f"    ❌ Opponent parse exception: '{opponent_str}' - {e}")
            return None
            
        # Determine home/away teams
        home_team_abbrev = team_abbrev if is_home else opponent_abbrev
        away_team_abbrev = opponent_abbrev if is_home else team_abbrev
        
        # Create team and venue objects
        try:
            home_team = self.create_team_info(home_team_abbrev, league)
            away_team = self.create_team_info(away_team_abbrev, league)
            venue = self.create_venue_info(home_team_abbrev, league)
        except Exception:
            return None
        
        # Set game status
        status = GameStatus.SCHEDULED if not result_str or result_str == '-' else GameStatus.FINAL
        game_id = f"{league}_{season}_{game_date.strftime('%Y%m%d')}_{away_team_abbrev}_{home_team_abbrev}"
        
        return GameData(
            game_id=game_id,
            date=game_date,
            home_team=home_team,
            away_team=away_team,
            venue=venue,
            status=status,
            league=league,
            season=season
        )
    
    def parse_date_for_league(self, date_str: str, league: str, season: str) -> Optional[datetime]:
        """Parse ESPN date format with league-specific logic"""
        if not date_str or not isinstance(date_str, str):
            return None
        
        try:
            
            if 'live' in date_str.lower() or date_str.strip() == '':
                return datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
            
            if ',' in date_str:
                date_part = date_str.split(',', 1)[1].strip()
            else:
                date_part = date_str.strip()
            
            skip_entries = ['tbd', 'postponed', 'cancelled', 'all-star', 'break']
            if any(skip in date_part.lower() for skip in skip_entries):
                return None
                
            parts = date_part.split()
            if len(parts) == 2:
                month_str, day_str = parts[0], parts[1]
            elif '/' in date_part:
                date_parts = date_part.split('/')
                if len(date_parts) == 2:
                    month_num, day_str = date_parts[0], date_parts[1]
                    month_str = self._number_to_month(int(month_num))
                    if not month_str:
                        return None
                else:
                    return None
            else:
                return None
            
            month_map = {
                'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
                'January': 1, 'February': 2, 'March': 3, 'April': 4, 
                'May': 5, 'June': 6, 'July': 7, 'August': 8, 
                'September': 9, 'October': 10, 'November': 11, 'December': 12
            }
            
            month = month_map.get(month_str)
            if not month:
                return None
            
            day_str = re.sub(r'[^0-9]', '', day_str)
            if not day_str:
                return None
                
            day = int(day_str)
            if day < 1 or day > 31:
                return None
            
            if league in ['NBA', 'NHL'] and '-' in season:
                start_year, end_year_short = season.split('-')
                start_year = int(start_year)
                end_year = int('20' + end_year_short)
                
                if month >= 10:
                    year = start_year
                else:
                    year = end_year
            else:  # MLB
                year = int(season)
                
                current_date = datetime.now()
                if month <= 3 and current_date.month >= 10:
                    year += 1
            
            default_hours = {
                'MLB': 19,
                'NBA': 20,
                'NHL': 19
            }
            default_hour = default_hours.get(league, 19)
                
            return datetime(year, month, day, default_hour, 0)
            
        except Exception as e:
            return None
    
    def _number_to_month(self, month_num: int) -> Optional[str]:
        """Convert month number to abbreviated month name"""
        month_names = {
            1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
            7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'
        }
        return month_names.get(month_num)
    
    def parse_opponent(self, opponent_str: str, league: str) -> Tuple[bool, Optional[str]]:
        """Parse opponent string with league-specific team name resolution - FIXED with href parsing"""
        if not opponent_str:
            return False, None
            
        if opponent_str.startswith('vs'):
            is_home = True
            opponent_name = opponent_str[2:].strip()
        elif opponent_str.startswith('@'):
            is_home = False
            opponent_name = opponent_str[1:].strip()
        else:
            is_home = True
            opponent_name = opponent_str.strip()
        
        # Strip special characters like asterisks
        opponent_name = re.sub(r'[*#^&]', '', opponent_name).strip()
            
        opponent_abbrev = self.get_team_abbrev_from_name(opponent_name, league)
        
        return is_home, opponent_abbrev
    
    def get_team_abbrev_from_name(self, team_name: str, league: str) -> Optional[str]:
        """Convert team city/name to abbreviation for specific league - FIXED for ESPN edge cases"""
        if not team_name:
            return None
            
        team_name = team_name.strip().lower()
        league_teams = self.league_teams.get(league, {})
        
        name_to_abbrev = {}
        
        # Build comprehensive mapping
        for abbrev, team_info in league_teams.items():
            city = team_info['city'].lower()
            full_name = team_info['name'].lower()
            
            # Add full team name (highest priority)
            name_to_abbrev[full_name] = abbrev
            
            # Add nickname (second priority)
            name_parts = full_name.split()
            if len(name_parts) > 1:
                nickname = name_parts[-1]
                if nickname not in name_to_abbrev or full_name in team_name:
                    name_to_abbrev[nickname] = abbrev
            
            # Add city ONLY if it's unique within the league
            city_count = sum(1 for t in league_teams.values() if t['city'].lower() == city)
            if city_count == 1:
                name_to_abbrev[city] = abbrev
            
            # Add abbreviation
            name_to_abbrev[abbrev.lower()] = abbrev
        
        # FIXED: Enhanced mapping for problematic cases
        if league == 'MLB':
            specific_mappings = {
                # Standard team mappings
                'mets': 'nym', 'new york mets': 'nym', 'yankees': 'nyy', 'new york yankees': 'nyy',
                'dodgers': 'lad', 'los angeles dodgers': 'lad', 'angels': 'laa', 'los angeles angels': 'laa',
                'cubs': 'chc', 'chicago cubs': 'chc', 'white sox': 'chw', 'chicago white sox': 'chw',
                
                
                'new nyy': 'nyy', 'new nym': 'nym',  # "New NYY" -> Yankees, "New NYM" -> Mets
                'los laa': 'laa', 'los lad': 'lad',  # "Los LAA" -> Angels, "Los LAD" -> Dodgers  
                'chi chc': 'chc', 'chi chw': 'chw',  # Chicago teams (potential)
                
                # Handle concatenated versions too
                'newnyy': 'nyy', 'newnym': 'nym', 'loslaa': 'laa', 'loslad': 'lad',
                
                # Handle spacing variations
                'vsnew nyy': 'nyy', 'vsnew nym': 'nym', 'vslos laa': 'laa', 'vslos lad': 'lad',
                '@new nyy': 'nyy', '@new nym': 'nym', '@los laa': 'laa', '@los lad': 'lad',
            }
            name_to_abbrev.update(specific_mappings)
            
        elif league == 'NBA':
            specific_mappings = {
                'lakers': 'lal', 'clippers': 'lac', 'knicks': 'ny', 'nets': 'bkn', 'bulls': 'chi',
                # NBA malformed fixes (if they occur)
                'new ny': 'ny', 'los lal': 'lal', 'los lac': 'lac',
            }
            name_to_abbrev.update(specific_mappings)
            
        elif league == 'NHL':
            specific_mappings = {
                'kings': 'la', 'rangers': 'nyr', 'islanders': 'nyi', 'blackhawks': 'chi',
                # NHL malformed fixes (if they occur)  
                'new nyr': 'nyr', 'new nyi': 'nyi', 'los la': 'la',
            }
            name_to_abbrev.update(specific_mappings)
        
        # Try exact match first
        if team_name in name_to_abbrev:
            return name_to_abbrev[team_name]
        
        # Try cleaned name (remove non-alphabetic characters)
        clean_name = re.sub(r'[^a-z]', '', team_name)
        if clean_name in name_to_abbrev:
            return name_to_abbrev[clean_name]
        
        # Try partial matching
        for name_key, abbrev in name_to_abbrev.items():
            if clean_name in name_key or name_key in clean_name:
                return abbrev
        
        # Debug output for still-unmatched teams
        logger.warning(f"⚠️  Could not match '{team_name}' to any {league} team")
        return None
    
    def create_team_info(self, team_abbrev: str, league: str) -> TeamInfo:
        """Create TeamInfo object from abbreviation and league"""
        league_teams = self.league_teams.get(league, {})
        team_data = league_teams.get(team_abbrev.lower(), {})
        
        return TeamInfo(
            team_id=team_abbrev.lower(),
            abbreviation=team_abbrev.upper(),
            display_name=team_data.get('name', f'{league} Team {team_abbrev.upper()}'),
            location=team_data.get('city', ''),
            color='#000000',
            alternate_color='#FFFFFF',
            division=team_data.get('division', ''),
            league=league,
            conference=team_data.get('conference', '')
        )
    
    def create_venue_info(self, home_team_abbrev: str, league: str) -> Venue:
        """Create Venue object for home team's venue"""
        league_teams = self.league_teams.get(league, {})
        team_data = league_teams.get(home_team_abbrev.lower(), {})
        city = team_data.get('city', '')
        coords = self.city_coordinates.get(city, (0.0, 0.0))
        
        venue_suffixes = {
            'MLB': 'Stadium',
            'NBA': 'Arena', 
            'NHL': 'Arena'
        }
        
        venue_suffix = venue_suffixes.get(league, 'Stadium')
        
        return Venue(
            venue_id=f"{home_team_abbrev}_{league}_venue",
            name=f"{city} {venue_suffix}",
            city=city,
            state='',
            country='USA' if city not in ['Toronto', 'Montreal', 'Vancouver', 'Calgary', 'Edmonton', 'Ottawa', 'Winnipeg'] else 'Canada',
            latitude=coords[0],
            longitude=coords[1]
        )


class TravelInferenceEngine:
    """Engine to infer team travel patterns from game schedules - VENUE_ID CHANGES ONLY"""
    
    def __init__(self, airport_mappings: Dict[str, str]):
        self.airport_mappings = airport_mappings
    
    def infer_travel_from_games(self, games: List[GameData], focus_team_id: str, league: str) -> List[TeamTravelData]:
        """Process ONLY the focus team - not all 30 teams"""
        
        logger.debug(f"🐛 DEBUG: focus_team_id = '{focus_team_id}', league = '{league}'")
        logger.debug(f"🐛 DEBUG: Total games passed in = {len(games)}")
        
        # Get only games where the focus team plays
        team_games = []
        for game in games:
            if game.home_team.team_id == focus_team_id or game.away_team.team_id == focus_team_id:
                team_games.append(game)
        
        logger.debug(f"🐛 DEBUG: Games for team '{focus_team_id}' = {len(team_games)}")
        
        # Sort by date
        team_games.sort(key=lambda x: x.date)
        
        # Process venue changes for JUST this team
        travel_results = self._infer_venue_changes(focus_team_id, team_games, league)
        logger.debug(f"🐛 DEBUG: Travel records generated = {len(travel_results)}")
        
        return travel_results

    
    def _infer_venue_changes(self, team_id: str, games_list: List[GameData], league: str) -> List[TeamTravelData]:
        """Generate travel records ONLY when venue_id changes between consecutive games"""
        travel_data = []
        
        if len(games_list) < 2:
            return travel_data
        
        # Travel patterns for timing
        travel_patterns = {
            'MLB': {'advance_days': 1},
            'NBA': {'advance_days': 1}, 
            'NHL': {'advance_days': 1}
        }
        pattern = travel_patterns.get(league, travel_patterns['MLB'])
        
        logger.debug(f"🔍 Processing {len(games_list)} games for team {team_id}")
        
        # Compare consecutive games
        venue_changes = 0
        for i in range(len(games_list) - 1):
            current_game = games_list[i]
            next_game = games_list[i + 1]
            
            # ONLY create travel record if venue_id changes
            if current_game.venue.venue_id != next_game.venue.venue_id:
                venue_changes += 1
                
                # Get team info (consistent across all games for this team)
                team_info = current_game.home_team if current_game.home_team.team_id == team_id else current_game.away_team
                
                # Travel FROM current game venue TO next game venue
                departure_city = current_game.venue.city
                arrival_city = next_game.venue.city
                
                if departure_city and arrival_city and departure_city != arrival_city:
                    # Travel date = next game date minus advance days
                    travel_date = next_game.date - timedelta(days=pattern['advance_days'])
                    
                    # Clean game date
                    game_date = next_game.date
                    if hasattr(game_date, 'tzinfo') and game_date.tzinfo is not None:
                        game_date = game_date.replace(tzinfo=None)
                    
                    # Get airports
                    dep_airport = self.airport_mappings.get(departure_city, departure_city[:3].upper())
                    arr_airport = self.airport_mappings.get(arrival_city, arrival_city[:3].upper())
                    
                    # Determine opponent for next game
                    opponent = next_game.home_team.display_name if team_info.team_id != next_game.home_team.team_id else next_game.away_team.display_name
                    
                    travel_record = TeamTravelData(
                        team_name=team_info.display_name,
                        team_id=team_info.team_id,
                        departure_city=departure_city,
                        arrival_city=arrival_city,
                        game_date=game_date,
                        travel_date=travel_date,
                        departure_airport=dep_airport,
                        arrival_airport=arr_airport,
                        confidence="venue_change_inferred",
                        game_id=next_game.game_id,
                        opponent=opponent
                    )
                    
                    travel_data.append(travel_record)
                    logger.debug(f"   ✈️  {departure_city} → {arrival_city} (venue changed)")
        
        logger.debug(f"   📊 Team {team_id}: {venue_changes} venue changes = {len(travel_data)} travel records")
        return travel_data


class _LoaderWorker(QThread):
    """Runs one schedule-load callable off the GUI thread. Results flow back
    through the aggregator's existing signals (queued to the main thread)."""

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self._fn()
        except Exception as e:
            logger.exception("Background load failed: %s", e)


class ESPNSportsDataAggregator(QObject):
    """Multi-league ESPN sports data aggregator with database integration"""
    
    
    dataUpdated = pyqtSignal(list)
    progressUpdated = pyqtSignal(int)
    errorOccurred = pyqtSignal(str)
    seasonDataLoaded = pyqtSignal(str, str, int)  # season, league, game_count
    scheduleRefreshed = pyqtSignal(str, str, dict)  # league, season, summary
    amadeusIntelligenceReady = pyqtSignal(object)  # Flight/Airport/Hotel data
    amadeusProgressUpdated = pyqtSignal(int, str)
    
    def __init__(self, config: Dict[str, str], db_path: str = "sports_data.db"):
        super().__init__()
        
        # Check if DB file exists; if not, scrape it all
        self.amadeus_worker = None
        db_exists = Path(db_path).exists()
        
        self.db = DatabaseManager(db_path)
        self.espn_scraper = ESPNScheduleScraper()
        self.inference_engine = TravelInferenceEngine(self.espn_scraper.team_airports)
        
        self.current_league = "MLB"
        self.current_season = None
        self.all_season_games = []
        self.current_travel_data = []
        self.teams_cache = {}

        # Single background worker for schedule loads. Loads can cascade into
        # a full ESPN scrape (minutes), which must never block the GUI thread.
        self._loader_worker = None

        # Flashscore: fast rolling-window refresh + live scores. Lazy client;
        # only touched from the loader worker / poller threads.
        from flashscore_source import FlashscoreScheduleSource
        self.flashscore = FlashscoreScheduleSource(self.db)

        self.load_teams_for_all_leagues()
        self.set_league(self.current_league)

    def _start_loader(self, fn, description: str) -> bool:
        """Run a load function on a background thread. One at a time; returns
        False (and emits a status error) if a load is already running."""
        if self._loader_worker is not None and self._loader_worker.isRunning():
            logger.warning("Loader busy, ignoring request: %s", description)
            return False

        # No deleteLater: we hold the only reference, and the next assignment
        # releases the finished worker. deleteLater would leave a dead wrapper
        # whose isRunning() raises.
        worker = _LoaderWorker(fn, self)
        self._loader_worker = worker
        worker.start()
        logger.debug("Started background load: %s", description)
        return True

    def shutdown(self):
        """Wait for any in-flight background load before teardown."""
        if self._loader_worker is not None and self._loader_worker.isRunning():
            logger.info("Waiting for background schedule load to finish...")
            self._loader_worker.wait(5000)

    def set_league(self, league: str):
        """Set current league and update current season"""
        if league not in ['MLB', 'NBA', 'NHL']:
            raise ValueError(f"Unsupported league: {league}")
        
        self.current_league = league
        self.current_season = self.espn_scraper.get_current_season_for_league(league)
        logger.debug(f"Set league to {league}, current season: {self.current_season}")
    
    
    def set_amadeus_credentials(self, api_key: str, api_secret: str):
        """Set up Amadeus integration with aggregator reference"""
        try:
            # Pass self (aggregator) to AmadeusAnalyzer for access to city coordinates
            self.amadeus_analyzer = AmadeusAnalyzer(api_key, api_secret, aggregator=self)
            return True
        except Exception as e:
            logger.debug(f"Failed to initialize Amadeus: {e}")
            return False
    
    
    def load_teams_for_all_leagues(self):
        """Load team information for all leagues"""
        supported_leagues = ['MLB', 'NBA', 'NHL']
        
        for league in supported_leagues:
            teams = self.db.load_teams(league)
            
            if not teams:
                logger.debug(f"No {league} teams found in database, initializing from scraper...")
                teams = []
                league_teams = self.espn_scraper.league_teams.get(league, {})
                
                for abbrev, info in league_teams.items():
                    team = TeamInfo(
                        team_id=abbrev,
                        abbreviation=abbrev.upper(),
                        display_name=info['name'],
                        location=info['city'],
                        color='#000000',
                        alternate_color='#FFFFFF',
                        division=info['division'],
                        league=league,
                        conference=info.get('conference', '')
                    )
                    teams.append(team)
                
                if teams:
                    self.db.save_teams(teams, league)
            
            self.teams_cache[league] = {team.team_id: team for team in teams}
            logger.debug(f"Loaded {len(teams)} {league} teams")
    
    def load_full_season_schedule(self, season: str = None, league: str = None,
                                  force_refresh: bool = False) -> bool:
        """Load complete season schedule on a background thread.

        May cascade into a full ESPN scrape; never blocks the GUI. Completion
        is reported via seasonDataLoaded / dataUpdated / errorOccurred."""
        return self._start_loader(
            lambda: self._load_full_season_schedule_sync(season, league, force_refresh),
            f"full season {league} {season}")

    def load_team_season_schedule(self, team_id: str, season: str = None,
                                  league: str = None) -> bool:
        """Load one team's schedule on a background thread."""
        return self._start_loader(
            lambda: self._load_team_season_schedule_sync(team_id, season, league),
            f"team {team_id} {league} {season}")

    def get_current_week_schedule(self, league: str = None) -> bool:
        """Load the current week's travel on a background thread."""
        return self._start_loader(
            lambda: self._get_current_week_schedule_sync(league),
            f"current week {league}")

    def refresh_upcoming_schedule(self, league: str = None, days_ahead: int = 7) -> bool:
        """Flashscore rolling-window refresh on a background thread (~1s).

        Fixes dates/postponements/statuses for the upcoming window, inserts
        newly scheduled games (playoffs!), bumps the cache freshness stamp so
        the staleness check stops cascading into ESPN re-scrapes, and rebuilds
        travel inference when game days actually moved. Completion is reported
        via scheduleRefreshed(league, season, summary)."""
        return self._start_loader(
            lambda: self._refresh_upcoming_schedule_sync(league, days_ahead),
            f"flashscore refresh {league}")

    def _refresh_upcoming_schedule_sync(self, league: str = None, days_ahead: int = 7):
        if league is None:
            league = self.current_league
        season = self.espn_scraper.get_current_season_for_league(league)

        try:
            summary = self.flashscore.refresh_upcoming(
                league, season, days_ahead=days_ahead)
        except Exception as e:
            logger.warning("Flashscore refresh failed for %s: %s", league, e)
            return

        if summary.get("travel_relevant"):
            self._rebuild_travel_sync(season, league)

        self.scheduleRefreshed.emit(league, season, summary)

    def _rebuild_travel_sync(self, season: str, league: str):
        """Re-run travel inference for every team from the games table."""
        games = self.db.load_games(season, league)
        if not games:
            return
        travel_data = []
        for team in self.get_all_teams(league):
            travel_data.extend(
                self.inference_engine.infer_travel_from_games(games, team.team_id, league))
        self.db.save_travel_data(travel_data, season, league)
        logger.info("Rebuilt %d travel records for %s %s", len(travel_data), league, season)

    def _load_full_season_schedule_sync(self, season: str = None, league: str = None, force_refresh: bool = False):
        """Load complete season schedule for specified league"""
        if league is None:
            league = self.current_league
        
        if season is None:
            season = self.espn_scraper.get_current_season_for_league(league)
        
        try:
            if not self.db.should_refresh_season(season, league, force_refresh):
                logger.debug(f"Loading {league} {season} season from database cache...")
                self.progressUpdated.emit(20)
                
                games = self.db.load_games(season, league)
                travel_data = self.db.load_travel_data(season, league)
                
                if games and travel_data:
                    self.all_season_games = games
                    self.current_travel_data = travel_data
                    self.dataUpdated.emit(travel_data)
                    self.seasonDataLoaded.emit(season, league, len(games))
                    self.progressUpdated.emit(100)
                    logger.debug(f"Loaded {len(games)} {league} games and {len(travel_data)} travel records from database")
                    return
                else:
                    logger.debug(f"No cached {league} data found for {season}, will scrape...")
            
            logger.debug(f"Scraping {league} {season} season schedule from ESPN...")
            self.progressUpdated.emit(10)
            
            #self.db.clear_season_data(season, league)
            
            season_games = self.espn_scraper.scrape_league_schedule(league, season)
            
            if season_games:
                self.progressUpdated.emit(60)
                
                self.db.save_games(season_games, season, league)
                self.progressUpdated.emit(70)
                
                
                travel_data = []
                all_teams = self.get_all_teams(league)  # Get all teams for this league
                
                logger.debug(f"🔄 Processing travel data for all {len(all_teams)} {league} teams...")
                
                for i, team in enumerate(all_teams, 1):
                    team_travel = self.inference_engine.infer_travel_from_games(season_games, team.team_id, league)
                    travel_data.extend(team_travel)
                    logger.info(f"  [{i}/{len(all_teams)}] ✅ {team.display_name}: {len(team_travel)} travel records")
                    
                    # Update progress during team processing
                    team_progress = 70 + int((i / len(all_teams)) * 15)  # Progress from 70% to 85%
                    self.progressUpdated.emit(team_progress)
                
                logger.debug(f"🏆 TOTAL: Generated {len(travel_data)} travel records for ALL {league} teams")
                
                self.db.save_travel_data(travel_data, season, league)
                self.progressUpdated.emit(90)
                
                self.all_season_games = season_games
                self.current_travel_data = travel_data
                
                self.dataUpdated.emit(travel_data)
                self.seasonDataLoaded.emit(season, league, len(season_games))
                
                logger.debug(f"Scraped and saved {len(season_games)} {league} games and {len(travel_data)} travel records")
                self.progressUpdated.emit(100)
            else:
                self.errorOccurred.emit(f"No {league} games found for {season} season")
                
        except Exception as e:
            error_msg = f"Failed to load {league} {season} season: {str(e)}"
            logger.debug(error_msg)
            self.errorOccurred.emit(error_msg)
            self.progressUpdated.emit(0)
    
    def _load_team_season_schedule_sync(self, team_id: str, season: str = None, league: str = None):
        """Load schedule for specific team"""
        if league is None:
            league = self.current_league
        
        if season is None:
            season = self.espn_scraper.get_current_season_for_league(league)
        
        try:
            self.progressUpdated.emit(20)
            
            team_travel = self.db.load_travel_data(season, league, team_id)
            
            if team_travel:
                logger.debug(f"Loaded {len(team_travel)} {league} travel records for {team_id} from database")
                self.dataUpdated.emit(team_travel)
                self.progressUpdated.emit(100)
                return
            
            is_cached, _ = self.db.is_season_cached(season, league)
            if not is_cached:
                self._load_full_season_schedule_sync(season, league)
                return
            
            team_travel = self.db.load_travel_data(season, league, team_id)
            if team_travel:
                self.dataUpdated.emit(team_travel)
                self.progressUpdated.emit(100)
            else:
                self.errorOccurred.emit(f"No {league} travel data found for team {team_id} in {season}")
                
        except Exception as e:
            self.errorOccurred.emit(f"Failed to load {league} team schedule: {str(e)}")
    
    def _get_current_week_schedule_sync(self, league: str = None):
        """Get current week games from database for specified league"""
        if league is None:
            league = self.current_league
        
        try:
            season = self.espn_scraper.get_current_season_for_league(league)
            
            is_cached, _ = self.db.is_season_cached(season, league)
            
            if not is_cached:
                logger.debug(f"No {league} {season} season data found, loading full season...")
                self._load_full_season_schedule_sync(season, league)
                return
            
            today = datetime.now()
            week_start = today - timedelta(days=3)
            week_end = today + timedelta(days=4)
            
            all_travel = self.db.load_travel_data(season, league)
            current_week_travel = [
                travel for travel in all_travel
                if week_start <= travel.travel_date <= week_end
            ]
            
            if current_week_travel:
                self.dataUpdated.emit(current_week_travel)
                logger.debug(f"Loaded {len(current_week_travel)} {league} travel records for current week")
            else:
                self.errorOccurred.emit(f"No {league} current week travel found")
                
        except Exception as e:
            self.errorOccurred.emit(f"Failed to load {league} current week: {str(e)}")
    
    def get_travel_by_date_range(self, start_date: datetime, end_date: datetime, league: str = None) -> List[TeamTravelData]:
        """Filter travel data by date range"""
        if league is None:
            league = self.current_league
        
        try:
            season = self.espn_scraper.get_current_season_for_league(league)
            all_travel = self.db.load_travel_data(season, league)
            
            filtered_travel = [
                travel for travel in all_travel
                if start_date <= travel.travel_date <= end_date
            ]
            
            return filtered_travel
            
        except Exception as e:
            logger.debug(f"Error filtering {league} travel by date range: {e}")
            return []
    
     
    def get_team_travel_intelligence_async(self, team_abbr: str, days_ahead: int = 7) -> None:
        """Get enhanced travel data with Amadeus intelligence - SIMPLIFIED VERSION"""
        if not hasattr(self, 'amadeus_analyzer') or not self.amadeus_analyzer:
            self.errorOccurred.emit("Amadeus not configured")
            return
        
        # Cancel existing worker if running
        if hasattr(self, 'amadeus_worker') and self.amadeus_worker is not None and self.amadeus_worker.isRunning():
            self.amadeus_worker.cancel()
            self.amadeus_worker.wait()
        
        # Create worker
        self.amadeus_worker = AmadeusWorker(self, team_abbr, days_ahead)
        
        # IMPORTANT: Connect signals BEFORE starting the worker
        self.amadeus_worker.intelligenceReady.connect(self.amadeusIntelligenceReady.emit)
        self.amadeus_worker.errorOccurred.connect(self.errorOccurred.emit)
        self.amadeus_worker.progressUpdated.connect(self.amadeusProgressUpdated.emit)
        
        # Start the worker thread
        self.amadeus_worker.start()
        
        
    def get_team_info(self, team_id: str, league: str = None) -> Optional[TeamInfo]:
        """Get team information by ID and league"""
        if league is None:
            league = self.current_league
        
        league_teams = self.teams_cache.get(league, {})
        return league_teams.get(team_id.lower())
    
    def get_all_teams(self, league: str = None) -> List[TeamInfo]:
        """Get all cached teams for specified league"""
        if league is None:
            league = self.current_league
        
        league_teams = self.teams_cache.get(league, {})
        return list(league_teams.values())
    
    def get_supported_leagues(self) -> List[str]:
        """Get list of supported leagues"""
        return list(self.teams_cache.keys())
    
    def get_database_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        return self.db.get_database_stats()
    
    def get_cached_seasons(self, league: str = None) -> List[Dict[str, Any]]:
        """Get information about cached seasons for specified league"""
        if league is None:
            league = self.current_league
        
        return self.db.get_cached_seasons(league)
    
    def clear_season_cache(self, season: str, league: str = None):
        """Clear cache for specific season and league"""
        if league is None:
            league = self.current_league
        
        try:
            self.db.clear_season_data(season, league)
            logger.debug(f"Cleared {league} cache for {season} season")
        except Exception as e:
            logger.debug(f"Error clearing {league} season cache: {e}")
    
    


def run_scraper():
    """Run scraper for all leagues - concise multi-league version"""
    logger.debug("🏃‍♂️ Running ESPN Sports Scraper for All Leagues...")
    
    config = {}
    aggregator = ESPNSportsDataAggregator(config)
    
    # Scrape all supported leagues
    for league in ["NBA", "NHL", "MLB"]:# "NBA", "NHL", "MLB" 
        logger.debug(f"\n📊 Scraping {league}...")
        aggregator.set_league(league)
        aggregator.load_full_season_schedule(force_refresh=True)
    
    stats = aggregator.get_database_stats()
    logger.info(f"\n✅ Multi-league scraping complete. Database stats: {stats}")





if __name__ == "__main__":
    run_scraper()
