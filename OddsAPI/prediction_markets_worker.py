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
        self.update_interval = 1500  # 5 minutes between updates
        
    def run(self):
        """Main worker loop - runs in background thread"""
        while not self.should_stop:
            try:
                self.status_update.emit("Fetching prediction markets...")
                
                # Import and run polymarketquery in this thread
                try:
                    from polymarketquery import fetch_and_process_markets
                    import concurrent.futures
                    
                    # Check if we should stop before starting the long operation
                    if self.should_stop:
                        break
                    
                    # Create a shared cancellation flag
                    cancellation_flag = {'should_stop': False}
                    
                    # Use ThreadPoolExecutor with timeout for better control
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        # Submit the task with cancellation flag
                        future = executor.submit(fetch_and_process_markets, recent_only=True, cancellation_flag=cancellation_flag)
                        
                        try:
                            # Wait for completion with timeout, checking stop flag frequently
                            markets_data = None
                            timeout_seconds = 50  # 50 second timeout
                            check_interval = 1  # Check every second
                            
                            for i in range(timeout_seconds):
                                if self.should_stop:
                                    # Signal cancellation to the running operation
                                    cancellation_flag['should_stop'] = True
                                    print("🚫 Cancelling polymarket operation...")
                                    future.cancel()  # Try to cancel
                                    break
                                
                                try:
                                    markets_data = future.result(timeout=check_interval)
                                    break  # Got result
                                except concurrent.futures.TimeoutError:
                                    continue  # Keep waiting
                            
                            # Check again if we should stop after the operation
                            if self.should_stop:
                                break
                            
                            if markets_data:
                                # Format for tickertape display
                                formatted_markets = self.format_for_tickertape(markets_data)
                                
                                if formatted_markets and not self.should_stop:
                                    self.data_ready.emit(formatted_markets)
                                    self.status_update.emit(f"Loaded {len(formatted_markets)} prediction markets")
                                else:
                                    self.status_update.emit("No markets with sufficient volume found")
                            else:
                                if not self.should_stop:
                                    self.status_update.emit("No prediction market data received")
                                    
                        except Exception as e:
                            if not self.should_stop:
                                self.error_occurred.emit(f"Error during market fetch: {e}")
                        
                except ImportError as e:
                    self.error_occurred.emit(f"Failed to import pmapicall: {e}")
                except Exception as e:
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
        self.quit()
        # Don't wait indefinitely - give it 2 seconds max then force quit
        if not self.wait(2000):  # 2 second timeout
            print("Prediction markets worker didn't stop gracefully, terminating...")
            self.terminate()
            self.wait(1000)  # Wait up to 1 more second for termination
