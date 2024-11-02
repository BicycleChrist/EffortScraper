import tkinter as tk
from tkinter import ttk, messagebox
import json
import time
from datetime import datetime
import threading
from typing import Optional, Dict, List

class MarketTicker:
    def __init__(self, root):
        self.root = root
        self.root.title("Market Predictions Ticker")

        # Configuration
        self.config = {
            'update_interval': 30,  # seconds
            'max_ticker_length': 10000,
            'scroll_fps': 60,
            'default_speed': 1.0,
            'fonts': {
                'header': ('Helvetica', 12, 'bold'),
                'ticker': ('Helvetica', 16, 'bold'),
                'status': ('Helvetica', 10)
            },
            'colors': {
                'bg': 'black',
                'fg': 'white',
                'status': '#00ff00',
                'error': '#ff0000',
                'hot_market': '#ff9900'  # Color for close races
            },
            'spread_threshold': 20.0  # Highlight markets with spread below this
        }

        # State variables
        self.running = True
        self.ticker_text = ""
        self.last_update: Optional[datetime] = None
        self.pause_scroll = False
        self.last_scroll_time = time.time()

        # Configure the root window
        self.root.configure(bg=self.config['colors']['bg'])
        self.root.attributes('-topmost', True)

        # Create main frame
        self.main_frame = ttk.Frame(root)
        self.main_frame.pack(fill='both', expand=True)

        # Initialize UI
        self.create_styles()
        self.create_header()
        self.create_ticker()
        self.create_status_bar()
        self.create_detail_view()

        # Bind events
        self.setup_bindings()

        # Start background processes
        self.start_background_tasks()

    def create_styles(self):
        style = ttk.Style()
        style.configure('Header.TLabel',
                       background=self.config['colors']['bg'],
                       foreground=self.config['colors']['fg'],
                       font=self.config['fonts']['header'])
        style.configure('Status.TLabel',
                       background=self.config['colors']['bg'],
                       foreground=self.config['colors']['status'],
                       font=self.config['fonts']['status'])
        style.configure('Error.TLabel',
                       background=self.config['colors']['bg'],
                       foreground=self.config['colors']['error'],
                       font=self.config['fonts']['status'])
        style.configure('Hot.TLabel',
                       background=self.config['colors']['bg'],
                       foreground=self.config['colors']['hot_market'],
                       font=self.config['fonts']['status'])

    def create_header(self):
        header_frame = ttk.Frame(self.main_frame)
        header_frame.pack(fill='x', padx=5, pady=5)

        title_label = ttk.Label(header_frame,
                               text="PREDICTION MARKETS LIVE",
                               style='Header.TLabel')
        title_label.pack(side='left')

        # Controls
        controls_frame = ttk.Frame(header_frame)
        controls_frame.pack(side='right')

        ttk.Label(controls_frame,
                 text="Speed:",
                 style='Header.TLabel').pack(side='left', padx=5)

        self.speed_var = tk.DoubleVar(value=self.config['default_speed'])
        speed_scale = ttk.Scale(controls_frame,
                              from_=0.5,
                              to=8.0,
                              variable=self.speed_var,
                              orient='horizontal',
                              length=100,
                              command=self.on_speed_change)
        speed_scale.pack(side='left', padx=5)

    def create_ticker(self):
        self.canvas = tk.Canvas(self.main_frame,
                              height=50,
                              bg=self.config['colors']['bg'],
                              highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)

        self.text_id = self.canvas.create_text(
            0, 25,
            text="Loading market data...",
            anchor='w',
            fill=self.config['colors']['fg'],
            font=self.config['fonts']['ticker']
        )

    def create_status_bar(self):
        self.status_frame = ttk.Frame(self.main_frame)
        self.status_frame.pack(fill='x', padx=5, pady=2)

        self.status_label = ttk.Label(self.status_frame,
                                    text="Last updated: Never",
                                    style='Status.TLabel')
        self.status_label.pack(side='left')

        self.hot_markets_label = ttk.Label(self.status_frame,
                                         text="Hot Markets: 0",
                                         style='Hot.TLabel')
        self.hot_markets_label.pack(side='right', padx=(0, 10))

        self.market_count_label = ttk.Label(self.status_frame,
                                          text="Markets: 0",
                                          style='Status.TLabel')
        self.market_count_label.pack(side='right')

    def create_detail_view(self):
        self.detail_window = None

    def setup_bindings(self):
        self.canvas.bind('<Enter>', self.pause_scrolling)
        self.canvas.bind('<Leave>', self.resume_scrolling)
        self.canvas.bind('<Button-1>', self.on_click)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def start_background_tasks(self):
        self.update_thread = threading.Thread(target=self.update_data_periodically,
                                           daemon=True)
        self.update_thread.start()
        self.scroll_text()

    def calculate_spread(self, market: Dict) -> float:
        """Calculate the spread between Yes/No or highest/lowest options"""
        try:
            # Extract percentages from lines
            percentages = []
            for line in market['lines']:
                try:
                    value = float(line.split(':')[1].strip().replace('%', ''))
                    percentages.append(value)
                except (ValueError, IndexError):
                    continue

            if len(percentages) < 2:
                return float('inf')  # Return infinity for invalid/incomplete markets

            # Sort percentages in descending order
            percentages.sort(reverse=True)
            # Calculate the spread
            spread = abs(percentages[0] - percentages[1])

            # Filter out markets with a spread of less than 0.1%
            if spread < 0.1:
                return float('inf')  # Treat markets with small spreads as invalid

            return spread
        except Exception:
            return float('inf')

    def sort_markets_by_spread(self, markets: List[Dict]) -> List[Dict]:
        """Sort markets by their spread, from lowest to highest"""
        # Add spread calculation to each market
        for market in markets:
            market['spread'] = self.calculate_spread(market)

        # Sort by spread and filter out invalid markets
        valid_markets = [m for m in markets if m['spread'] != float('inf')]
        return sorted(valid_markets, key=lambda x: x['spread'])

    def format_market_text(self, market: Dict) -> str:
        """Format individual market data with colors and styling"""
        try:
            question = market['question']
            lines = [f"{line}" for line in market['lines']]
            spread = market.get('spread', float('inf'))

            # Add a hot market indicator for close races
            prefix = "🔥 " if spread < self.config['spread_threshold'] else ""

            return f"{prefix}{question} | {' • '.join(lines)} (Spread: {spread:.1f}%)"
        except KeyError as e:
            raise ValueError(f"Invalid market data format: missing {e}")

    def load_data(self) -> None:
        """Load and validate market data from file"""
        try:
            with open('PMdump.json', 'r') as file:
                data = json.load(file)

            if not data:
                raise ValueError("Empty data received")

            # Validate and sort data
            if not all(isinstance(item, dict) and 'question' in item
                      and 'lines' in item for item in data):
                raise ValueError("Invalid data format")

            # Sort markets by spread
            sorted_markets = self.sort_markets_by_spread(data)

            # Format ticker text with character limit
            formatted_items = []
            current_length = 0

            for market in sorted_markets:
                formatted_text = self.format_market_text(market)
                if current_length + len(formatted_text) > self.config['max_ticker_length']:
                    break
                formatted_items.append(formatted_text)
                current_length += len(formatted_text)

            self.ticker_text = " │ ".join(formatted_items)
            self.last_update = datetime.now()

            # Update UI
            self.canvas.itemconfig(self.text_id, text=self.ticker_text)

            # Update status with additional spread information
            close_markets = sum(1 for m in sorted_markets
                              if m['spread'] < self.config['spread_threshold'])
            self.update_status(len(data), close_markets)

        except FileNotFoundError:
            self.show_error("Data file not found")
        except json.JSONDecodeError:
            self.show_error("Invalid JSON format")
        except Exception as e:
            self.show_error(f"Error loading data: {str(e)}")

    def update_status(self, market_count: int, close_markets: int) -> None:
        """Update status bar with current information"""
        if self.last_update:
            self.status_label.config(
                text=f"Last updated: {self.last_update.strftime('%H:%M:%S')}",
                style='Status.TLabel'
            )
        self.market_count_label.config(text=f"Total Markets: {market_count}")
        self.hot_markets_label.config(text=f"Hot Markets: {close_markets}")

    def show_error(self, message: str) -> None:
        """Display error message in status bar and optionally in popup"""
        self.status_label.config(text=f"Error: {message}", style='Error.TLabel')
        messagebox.showerror("Error", message)

    def update_data_periodically(self) -> None:
        """Periodically update market data"""
        while self.running:
            self.load_data()
            time.sleep(self.config['update_interval'])

    def scroll_text(self) -> None:
        """Implement smooth scrolling with time-based movement"""
        if not self.pause_scroll:
            current_time = time.time()
            delta_time = current_time - self.last_scroll_time

            # Calculate movement based on time difference and speed
            speed = self.speed_var.get() * delta_time * 60  # pixels per second

            # Get text position
            x1, y1, x2, y2 = self.canvas.bbox(self.text_id)

            # Reset position if text has scrolled off screen
            if x2 < 0:
                self.canvas.move(self.text_id,
                               self.canvas.winfo_width() - x1,
                               0)
            else:
                self.canvas.move(self.text_id, -speed, 0)

            self.last_scroll_time = current_time

        # Schedule next update based on desired FPS
        self.root.after(int(1000 / self.config['scroll_fps']), self.scroll_text)

    def show_detail_view(self, text: str) -> None:
        """Show detailed view of clicked market"""
        if self.detail_window is None or not self.detail_window.winfo_exists():
            self.detail_window = tk.Toplevel(self.root)
            self.detail_window.title("Market Detail")
            self.detail_window.geometry("400x300")

            text_widget = tk.Text(self.detail_window,
                                wrap=tk.WORD,
                                padx=10,
                                pady=10)
            text_widget.pack(fill='both', expand=True)

            # Format the detail view text
            if "🔥" in text:
                text = f"HOT MARKET - Close Race!\n\n{text}"

            text_widget.insert('1.0', text)
            text_widget.config(state='disabled')

    def pause_scrolling(self, event) -> None:
        """Pause scrolling when mouse enters ticker area"""
        self.pause_scroll = True
        self.canvas.configure(cursor='hand2')

    def resume_scrolling(self, event) -> None:
        """Resume scrolling when mouse leaves ticker area"""
        self.pause_scroll = False
        self.canvas.configure(cursor='')

    def on_click(self, event) -> None:
        """Handle click events on the ticker"""
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)

        clicked_item = self.canvas.find_closest(x, y)
        if clicked_item:
            text = self.canvas.itemcget(clicked_item, 'text')
            self.show_detail_view(text)

    def on_speed_change(self, value) -> None:
        """Handle speed scale changes"""
        # Reset scroll timing on speed change to prevent jerky movement
        self.last_scroll_time = time.time()

    def on_closing(self) -> None:
        """Clean up resources before closing"""
        self.running = False
        if hasattr(self, 'update_thread'):
            self.update_thread.join(timeout=1.0)
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = MarketTicker(root)
    root.mainloop()
