import threading
import time
from PyQt6.QtCore import QThread, pyqtSignal

class PredictionMarketsWorker(QThread):
    """Background worker to fetch prediction market data without blocking UI"""

    # Signals for communication with main thread
    data_ready = pyqtSignal(list)  # Emits formatted prediction market data
    error_occurred = pyqtSignal(str)  # Emits error messages
    status_update = pyqtSignal(str)  # Emits status updates

    def __init__(self):
        super().__init__()
        self.should_stop = False
        self.update_interval = 1500  # 25 minutes between updates (loop sleeps 1s × 1500)

    def run(self):
        """Main worker loop - runs in background thread"""
        # Shared cancellation flag passed into polymarketquery so it can bail
        # out of pagination / volume fetches when the worker is asked to stop.
        self.cancellation_flag = {'should_stop': False}

        while not self.should_stop:
            try:
                self.status_update.emit("Fetching prediction markets...")

                try:
                    from polymarketquery import fetch_and_process_markets

                    if self.should_stop:
                        break

                    markets_data = fetch_and_process_markets(
                        recent_only=True,
                        cancellation_flag=self.cancellation_flag,
                    )

                    if self.should_stop:
                        break

                    if markets_data:
                        formatted_markets = self.format_for_tickertape(markets_data)
                        if formatted_markets and not self.should_stop:
                            self.data_ready.emit(formatted_markets)
                            self.status_update.emit(f"Loaded {len(formatted_markets)} prediction markets")
                        else:
                            self.status_update.emit("No markets with sufficient volume found")
                    else:
                        if not self.should_stop:
                            self.status_update.emit("No prediction market data received")

                except ImportError as e:
                    self.error_occurred.emit(f"Failed to import polymarketquery: {e}")
                except Exception as e:
                    if not self.should_stop:
                        self.error_occurred.emit(f"Error fetching markets: {e}")

                # Wait for next update or stop signal
                for _ in range(self.update_interval):
                    if self.should_stop:
                        break
                    time.sleep(1)

            except Exception as e:
                self.error_occurred.emit(f"Unexpected error in prediction markets worker: {e}")
                # Shorter retry delay when stopping
                retry_delay = 5 if self.should_stop else 30
                for _ in range(retry_delay):
                    if self.should_stop:
                        break
                    time.sleep(1)

    def format_for_tickertape(self, markets_data):
        """Format prediction market data for tickertape display"""
        formatted_markets = []

        # Take top 15 markets by volume for ticker display
        top_markets = markets_data[:15]

        for market in top_markets:
            try:
                # Extract market information
                question = market.get('question', 'Unknown Market')
                total_volume = market.get('total_volume', 0)
                lines = market.get('lines', [])

                # Format volume for display
                if total_volume >= 1000000:
                    volume_str = f"${total_volume/1000000:.1f}M"
                elif total_volume >= 1000:
                    volume_str = f"${total_volume/1000:.0f}K"
                else:
                    volume_str = f"${total_volume:.0f}"

                # Extract Yes/No prices
                yes_price = "?%"
                no_price = "?%"

                if len(lines) >= 2:
                    # Parse lines like ["Yes: 45.0%", "No: 55.0%"]
                    for line in lines:
                        if line.lower().startswith('yes:'):
                            yes_price = line.split(':')[1].strip()
                        elif line.lower().startswith('no:'):
                            no_price = line.split(':')[1].strip()

                # Truncate long questions for display
                display_question = question
                if len(display_question) > 60:
                    display_question = display_question[:57] + "..."

                # Format: "Question - Yes: 45% | No: 55% | $125K vol"
                formatted_text = f"{display_question} - Yes: {yes_price} | No: {no_price} | {volume_str} vol"
                formatted_markets.append(formatted_text)

            except Exception as e:
                print(f"Error formatting market {market.get('question', 'Unknown')}: {e}")
                continue

        return formatted_markets

    def stop(self):
        """Signal the worker to stop gracefully"""
        self.should_stop = True
        if hasattr(self, 'cancellation_flag'):
            self.cancellation_flag['should_stop'] = True
        self.quit()
        # 500ms is enough for the sleep(1) loop to notice should_stop; the
        # app is exiting at this point so we don't need to confirm termination.
        if not self.wait(500):
            self.terminate()
