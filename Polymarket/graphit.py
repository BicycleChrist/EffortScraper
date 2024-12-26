from pmapicall import *
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from matplotlib.widgets import Button

# TODO: option for old multi-window display instead of subplot
# TODO: Allow for faster navigation between the different graph pages
# label is the text to put in the legend

# Worthwhile implementation of a class in python...I think
class PaginatedPlots:
    def __init__(self, plots_per_page=16):
        self.plots_per_page = plots_per_page
        self.current_page = 0
        self.market_data = []
        self.total_pages = 0
        self.fig = None
        self.axes = {}

    def add_market_data(self, market, timeseries_pair, line_labels):
        """Store market data for plotting"""
        self.market_data.append({
            'market': market,
            'timeseries_pair': timeseries_pair,
            'line_labels': line_labels
        })
        self.total_pages = (len(self.market_data) + self.plots_per_page - 1) // self.plots_per_page

    def plot_page(self):
        """Plot current page of markets"""
        plt.close('all')
        
        # Calculate start and end indices for current page
        start_idx = self.current_page * self.plots_per_page
        end_idx = min(start_idx + self.plots_per_page, len(self.market_data))
        
        # Create figure with space for navigation buttons
        self.fig = plt.figure(figsize=(15, 12))
        
        # Add navigation buttons
        next_btn = plt.axes([0.95, 0.02, 0.02, 0.04])
        prev_btn = plt.axes([0.90, 0.02, 0.02, 0.04])
        self.next_button = Button(next_btn, '→')
        self.prev_button = Button(prev_btn, '←')
        
        # Add page indicator
        plt.figtext(0.5, 0.02, f'Page {self.current_page + 1} of {self.total_pages}',
                   ha='center')
        
        # Calculate grid layout
        cols = min(3, self.plots_per_page)
        rows = (self.plots_per_page + cols - 1) // cols

        # Plot markets for current page
        for i, idx in enumerate(range(start_idx, end_idx)):
            data = self.market_data[idx]
            market = data['market']
            timeseries_pair = data['timeseries_pair']
            line_labels = data['line_labels']
            
            ax = plt.subplot(rows, cols, i + 1)
            
            for label, timeseries in zip(line_labels, timeseries_pair):
                df = pd.DataFrame(timeseries)
                df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
                
                ax.plot(df['datetime'], df['price'], linewidth=2, label=label)
                ax.set_title(market['question'], fontsize=10, pad=2)
                ax.grid(True, linestyle='--', alpha=0.7)
                
                # Format x-axis
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                ax.tick_params(axis='x', labelsize=6)
                
                # Add price statistics
                min_price = df['price'].min()
                max_price = df['price'].max()
                current_price = df['price'].iloc[-1]
                
                info_text = f'Current: {current_price:.3f}\nMin: {min_price:.3f}\nMax: {max_price:.3f}'
                props = dict(boxstyle='round', facecolor='white', alpha=0.8)
                ax.text(0.02, 0.98, info_text,
                       transform=ax.transAxes,
                       fontsize=7,
                       verticalalignment='top',
                       bbox=props)
            
            ax.legend(loc='upper right', bbox_to_anchor=(1, 1),
                     fancybox=True, shadow=True, fontsize=6)

        # Set up button callbacks
        self.next_button.on_clicked(self.next_page)
        self.prev_button.on_clicked(self.prev_page)
        
        plt.tight_layout()
        plt.subplots_adjust(bottom=0.1)  # Make room for navigation
        plt.show(block=True)

    def next_page(self, event):
        """Handle next page button click"""
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.plot_page()

    def prev_page(self, event):
        """Handle previous page button click"""
        if self.current_page > 0:
            self.current_page -= 1
            self.plot_page()

def PlotMarkets(query:str, markets, single_graph=False, confirm=False, plots_per_page=12):
    matching_markets = [market for market in markets if query in market['question']]
    print(f"found {len(matching_markets)} markets matching '{query}'")
    
    if confirm:
        userinput = input("continue? (Y/N, List): ")
        if userinput.capitalize() == 'List':
            for market in matching_markets: print(market['question'])
            userinput = input("\ncontinue? (Y/N): ")
        if userinput.capitalize() != 'Y': print("cancelling"); return

    # Initialize paginated plots
    paginated = PaginatedPlots(plots_per_page)
    
    # Collect all market data
    for market in matching_markets:
        timeseries_pair = [GetPriceHistory(int(token_id), fidelity_hours=12) 
                          for token_id in market['token_ids']]
        line_labels = [line.split(':', maxsplit=1)[0] for line in market['lines']]
        paginated.add_market_data(market, timeseries_pair, line_labels)
    
    # Display first page
    if paginated.market_data:
        paginated.plot_page()
    
    print("done")
    return



# for multithreaded version. Bugged; fails to fetch timeseries for most markets
def fetch_price_histories(market) -> tuple:
    """Helper function to fetch price histories for a single market"""
    timeseries_pair = [GetPriceHistory(int(token_id), fidelity_hours=12) for token_id in market['token_ids']]
    line_labels = [line.split(':', maxsplit=1)[0] for line in market['lines']]
    return (market['question'], line_labels, timeseries_pair)

# multithreaded version, presntly broken. Need to find request limit in docs. 
# consistent 429 resppnse code
def PlotMarketsMultiThreaded(query:str, markets, single_graph=False, market_limit=500, max_workers=10):
    matching_markets = [market for market in markets if query in market['question']][:market_limit]
    print(f"Processing {len(matching_markets)} markets out of {len([m for m in markets if query in m['question']])} matches")
    
    userinput = input("continue? (Y/N, List): ")
    if userinput.capitalize() == 'List':
        for market in matching_markets: print(market['question'])
        userinput = input("\ncontinue? (Y/N): ")
    if userinput.capitalize() != 'Y': print("cancelling"); return

    plt.close('all')
    def process_market(market_data):
        index, market = market_data
        try:
            timeseries_pair = [GetPriceHistory(int(token_id), fidelity_hours=12) for token_id in market['token_ids']]
            line_labels = [line.split(':', maxsplit=1)[0] for line in market['lines']]
            title = market['question']
            
            for label, timeseries in zip(line_labels, timeseries_pair):
                plot_title = title if not single_graph else query
                plot_index = 0 if single_graph else index
                plot_label = f"{title}: {label}" if single_graph else label
                # plot_price_history(plot_index, plot_label, timeseries, plot_title)
                print(f"getting {title}: {label}")
        except Exception as e:
            print(f"Error processing market {index}: {str(e)}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(process_market, enumerate(matching_markets))
    
    # plt.show(block=True)
    print("done")


if __name__ == "__main__":
    markets = LoadJsonDump()
    #PlotMarkets('OpenSea', markets)
    PlotMarkets('NFL', markets, False)
    # PlotMarketsMultiThreaded("Trump", markets, market_limit=1000, max_workers=16)
 
