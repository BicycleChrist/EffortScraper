#!/usr/bin/env python3
"""
ProphetX Order Book Widget
Professional PyQt6 widget for viewing ProphetX exchange order books
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QSplitter, QFrame, QComboBox, QHeaderView
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QPalette
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import ProphetXQuery


class OrderBookWidget(QWidget):
    """
    Professional order book display widget for ProphetX markets.
    Shows bid/ask ladder with liquidity depth similar to Polymarket.
    """

    def __init__(self, parent=None, compact_mode=False):
        super().__init__(parent)
        self.current_market = None
        self.current_line = None
        self.compact_mode = compact_mode
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout(self)
        margins = 2 if self.compact_mode else 0
        layout.setContentsMargins(margins, margins, margins, margins)
        layout.setSpacing(0)

        # Header showing market info (compact in compact mode)
        if not self.compact_mode:
            header = self.createHeader()
            layout.addWidget(header)
        else:
            header = self.createCompactHeader()
            layout.addWidget(header)

        # Order book table (all lines displayed)
        self.orderbook_table = QTableWidget()

        # In compact mode, hide some columns
        if self.compact_mode:
            self.orderbook_table.setColumnCount(3)
            self.orderbook_table.setHorizontalHeaderLabels([
                "SIDE", "ODDS", "LIQ"
            ])
        else:
            self.orderbook_table.setColumnCount(5)
            self.orderbook_table.setHorizontalHeaderLabels([
                "SELECTION", "ODDS", "LIQUIDITY", "CUMULATIVE", "% OF BOOK"
            ])

        # Styling
        self.orderbook_table.setAlternatingRowColors(False)
        self.orderbook_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.orderbook_table.setShowGrid(False)
        self.orderbook_table.verticalHeader().setVisible(False)
        self.orderbook_table.horizontalHeader().setStretchLastSection(True)

        # Set column widths
        header = self.orderbook_table.horizontalHeader()
        if self.compact_mode:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        else:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        # Font - smaller in compact mode with better font family
        font_size = 10 if self.compact_mode else 14
        book_font = QFont("SF Mono", font_size)
        book_font.setStyleHint(QFont.StyleHint.Monospace)
        book_font.setWeight(QFont.Weight.Medium)
        book_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.5)
        self.orderbook_table.setFont(book_font)

        # Row height - needs to fit font + padding in compact mode
        row_height = 28 if self.compact_mode else 50
        self.orderbook_table.verticalHeader().setDefaultSectionSize(row_height)

        # Minimize spacing in compact mode
        if self.compact_mode:
            self.orderbook_table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
            self.orderbook_table.verticalHeader().setMinimumSectionSize(26)

        layout.addWidget(self.orderbook_table)

        # Footer with spread info (compact or hidden in compact mode)
        if not self.compact_mode:
            self.footer_label = QLabel()
            self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.footer_label.setStyleSheet("""
                QLabel {
                    background-color: #1a1d24;
                    color: #8a92a3;
                    padding: 12px;
                    font-size: 14px;
                    border-top: 1px solid #2a2d34;
                }
            """)
            layout.addWidget(self.footer_label)
        else:
            self.footer_label = QLabel()
            self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.footer_label.setStyleSheet("""
                QLabel {
                    background-color: #1a1d24;
                    color: #8a92a3;
                    padding: 2px;
                    font-size: 8px;
                    border-top: 1px solid #2a2d34;
                }
            """)
            layout.addWidget(self.footer_label)

        # Styling - compact padding in compact mode
        padding = "4px 4px" if self.compact_mode else "12px"
        header_padding = "4px" if self.compact_mode else "12px"
        header_font_size = "9px" if self.compact_mode else "13px"

        self.setStyleSheet(f"""
            QTableWidget {{
                background-color: #0d0f14;
                border: none;
                color: #e8e9ed;
                gridline-color: transparent;
            }}
            QTableWidget::item {{
                padding: {padding};
                border: none;
                margin: 0px;
                background-color: rgba(26, 29, 36, 0.3);
            }}
            QHeaderView::section {{
                background-color: #1a1d24;
                color: #9ca3af;
                padding: {header_padding};
                border: none;
                font-weight: 600;
                font-size: {header_font_size};
                letter-spacing: 0.5px;
                text-transform: uppercase;
            }}
        """)

    def createHeader(self):
        """Create header widget showing current market name"""
        header = QFrame()
        header.setStyleSheet("""
            QFrame {
                background-color: #1a1d24;
                border-bottom: 1px solid #2a2d34;
            }
            QLabel {
                color: #ffffff;
                padding: 10px;
            }
        """)

        layout = QHBoxLayout(header)
        self.market_title = QLabel("Select a market to view order book")
        self.market_title.setFont(QFont("Arial", 15, QFont.Weight.Bold))
        layout.addWidget(self.market_title)

        self.stake_label = QLabel()
        self.stake_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.stake_label.setStyleSheet("color: #8a92a3; font-size: 13px;")
        layout.addWidget(self.stake_label)

        return header

    def createCompactHeader(self):
        """Create compact header for terminal integration"""
        header = QFrame()
        header.setStyleSheet("""
            QFrame {
                background-color: #1a1d24;
                border-bottom: 1px solid #2a2d34;
            }
            QLabel {
                color: #ffffff;
                padding: 1px;
            }
        """)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(8)

        self.market_title = QLabel("Select market")
        self.market_title.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        self.market_title.setWordWrap(True)
        layout.addWidget(self.market_title)

        layout.addStretch()

        self.stake_label = QLabel()
        self.stake_label.setStyleSheet("color: #8a92a3; font-size: 9px; font-weight: bold;")
        self.stake_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.stake_label)

        return header

    def setMarket(self, market_data: Dict):
        """
        Update order book display with new market data - shows ALL lines

        Args:
            market_data: Full market dict from ProphetX API
        """
        self.current_market = market_data

        # Update header
        market_name = market_data.get('name', 'Unknown Market')
        total_stake = market_data.get('totalStake', 0)
        self.market_title.setText(market_name)
        self.stake_label.setText(f"Total Liquidity: ${total_stake:,.2f}")

        # Render all lines from this market
        self.renderOrderBook(market_data)

    def renderOrderBook(self, market_data: Dict):
        """
        Render the order book showing ALL available lines with enhanced depth visualization

        Args:
            market_data: Full market dict from ProphetX API
        """
        all_orders = []

        # Check if this market has multiple lines (spread/total) or simple selections (moneyline)
        if 'marketLines' in market_data and market_data['marketLines']:
            # Spread/Total markets - collect all orders from all lines
            for market_line in market_data['marketLines']:
                line_name = market_line.get('name', '')
                selections = market_line.get('selections', [])

                # Each selection array is [side1_orders, side2_orders]
                for side_orders in selections:
                    if not side_orders:
                        continue
                    for order in side_orders:
                        all_orders.append(order)

        elif 'selections' in market_data:
            # Moneyline/simple markets
            selections = market_data.get('selections', [])
            for side_orders in selections:
                if not side_orders:
                    continue
                for order in side_orders:
                    all_orders.append(order)

        # Clear the table completely to avoid widget overlap issues
        self.orderbook_table.clearContents()
        self.orderbook_table.setRowCount(0)

        if not all_orders:
            self.orderbook_table.setRowCount(1)
            item = QTableWidgetItem("No orders available")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.orderbook_table.setItem(0, 0, item)
            self.orderbook_table.setSpan(0, 0, 1, 5)
            return

        # Separate into bids (negative odds/favorites) and asks (positive odds/underdogs)
        bids = [o for o in all_orders if o.get('odds', 0) < 0]
        asks = [o for o in all_orders if o.get('odds', 0) >= 0]

        # Sort bids by odds descending (best price first: -110 before -120)
        bids.sort(key=lambda x: x.get('odds', 0), reverse=True)

        # Sort asks by odds ascending (best price first: +110 before +120)
        asks.sort(key=lambda x: x.get('odds', 0))

        # Limit display per side
        max_per_side = 25
        asks = asks[:max_per_side]
        bids = bids[:max_per_side]

        # Combine: asks first (best to worst), separator, then bids (best to worst)
        all_orders = asks + bids
        insert_separator = len(asks) if (asks and bids) else -1

        # Set row count (add 1 for separator if needed)
        total_rows = len(all_orders) + (1 if insert_separator >= 0 else 0)
        self.orderbook_table.setRowCount(total_rows)

        # Calculate total book liquidity
        total_liquidity = sum(order.get('value', 0) for order in all_orders)

        # Find max stake for liquidity bar scaling
        max_stake = max(order.get('value', 1) for order in all_orders)

        # Calculate cumulative liquidity and render all orders
        cumulative = 0
        row_offset = 0
        for i, order in enumerate(all_orders):
            # Insert separator between asks and bids
            if i == insert_separator and insert_separator >= 0:
                self.renderSeparatorRow(i)
                row_offset = 1

            cumulative += order.get('value', 0)
            self.renderOrderRow(i + row_offset, order, max_stake, cumulative, total_liquidity)

        # Update footer with spread info
        if bids and asks:
            best_bid = bids[0]
            best_ask = asks[0]
            best_bid_odds = best_bid.get('odds', 0)
            best_ask_odds = best_ask.get('odds', 0)

            # Calculate implied probabilities and spread
            bid_prob = abs(best_bid_odds) / (abs(best_bid_odds) + 100) * 100 if best_bid_odds < 0 else 100 / (best_ask_odds + 100) * 100
            ask_prob = 100 / (best_ask_odds + 100) * 100 if best_ask_odds > 0 else abs(best_ask_odds) / (abs(best_ask_odds) + 100) * 100

            spread = abs(best_bid_odds - best_ask_odds)

            self.footer_label.setText(
                f"Showing {len(all_orders)} price levels • "
                f"Best Bid: {best_bid.get('displayOdds', 'N/A')} • "
                f"Best Ask: {best_ask.get('displayOdds', 'N/A')} • "
                f"Spread: {spread} points • "
                f"Total Liquidity: ${total_liquidity:,.2f}"
            )
        elif all_orders:
            best_order = all_orders[0]
            self.footer_label.setText(
                f"Showing {len(all_orders)} price levels • "
                f"Best: {best_order.get('displayName', 'N/A')} @ {best_order.get('displayOdds', 'N/A')} • "
                f"Total Liquidity: ${total_liquidity:,.2f}"
            )
        else:
            self.footer_label.setText("No orders available")

    def renderSeparatorRow(self, row: int):
        """Render a separator row between asks and bids showing the spread"""
        if self.compact_mode:
            separator = QTableWidgetItem("─ SPREAD ─")
            font_size = 9
            row_height = 24
            colspan = 3
        else:
            separator = QTableWidgetItem("───── SPREAD ─────")
            font_size = 12
            row_height = 40
            colspan = 5

        separator.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        sep_font = QFont("SF Mono", font_size, QFont.Weight.Bold)
        sep_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        separator.setFont(sep_font)
        separator.setForeground(QColor(96, 165, 250))  # Professional blue
        separator.setBackground(QColor(31, 41, 55))  # Darker gray-blue

        self.orderbook_table.setItem(row, 0, separator)
        self.orderbook_table.setSpan(row, 0, 1, colspan)

        # Set row height slightly larger for visual separation
        self.orderbook_table.setRowHeight(row, row_height)

    def renderOrderRow(self, row: int, order: Dict, max_stake: float, cumulative: float, total_liquidity: float):
        """Render a single order book row with team/selection, odds, liquidity, cumulative, and percentage"""
        display_name = order.get('displayName', order.get('abbreviatedName', '---'))
        display_odds = order.get('displayOdds', '---')
        value = order.get('value', 0)
        odds_value = order.get('odds', 0)

        # Determine if this is a favorite (negative odds) or underdog (positive odds)
        side_type = 'bid' if odds_value < 0 else 'ask'

        # Calculate liquidity bar width (0-100%)
        bar_width = int((value / max_stake) * 100) if max_stake > 0 else 0

        # Calculate percentage of total book
        percentage = (value / total_liquidity * 100) if total_liquidity > 0 else 0

        if self.compact_mode:
            # Compact mode: only 3 columns - SIDE, ODDS, LIQ
            # Use abbreviated name for side column
            side_name = order.get('abbreviatedName', display_name)
            # Shorten even more if too long
            if len(side_name) > 10:
                side_name = side_name[:8] + ".."

            selection_item = self.createSelectionItem(side_name, side_type)
            odds_widget = self.createLiquidityBarWidget(display_odds, bar_width, side_type)

            # Compact liquidity display
            if value >= 1000:
                liquidity_text = f"${value/1000:.1f}k"
            else:
                liquidity_text = f"${value:.0f}"
            liquidity_item = self.createPlainItem(liquidity_text)

            self.orderbook_table.setItem(row, 0, selection_item)
            self.orderbook_table.setCellWidget(row, 1, odds_widget)
            self.orderbook_table.setItem(row, 2, liquidity_item)
        else:
            # Full mode: all 5 columns
            selection_item = self.createSelectionItem(display_name, side_type)
            odds_widget = self.createLiquidityBarWidget(display_odds, bar_width, side_type)
            liquidity_item = self.createPlainItem(f"${value:,.2f}")
            cumulative_item = self.createPlainItem(f"${cumulative:,.2f}")
            percentage_item = self.createPercentageItem(f"{percentage:.1f}%", percentage)

            self.orderbook_table.setItem(row, 0, selection_item)
            self.orderbook_table.setCellWidget(row, 1, odds_widget)
            self.orderbook_table.setItem(row, 2, liquidity_item)
            self.orderbook_table.setItem(row, 3, cumulative_item)
            self.orderbook_table.setItem(row, 4, percentage_item)

    def createLiquidityBarWidget(self, odds: str, bar_width: int, side_type: str) -> QWidget:
        """
        Create a widget with odds text and liquidity bar background

        Args:
            odds: Odds string to display (e.g., "+164", "-190")
            bar_width: Width of liquidity bar as percentage (0-100)
            side_type: 'ask' or 'bid' for color coding
        """
        widget = QWidget()

        # Adjust layout and font size for compact mode
        if self.compact_mode:
            layout = QHBoxLayout(widget)
            layout.setContentsMargins(2, 1, 2, 1)
            layout.setSpacing(2)
            odds_font_size = 10
        else:
            layout = QHBoxLayout(widget)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setSpacing(12)
            odds_font_size = 16

        # Odds label
        odds_label = QLabel(odds)
        odds_font = QFont("SF Mono", odds_font_size, QFont.Weight.DemiBold)
        odds_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        odds_label.setFont(odds_font)

        if side_type == 'ask':
            # Refined red for asks - more professional crimson
            odds_label.setStyleSheet("color: #f87171; font-weight: 600;")
            bar_color = "248, 113, 113"  # Softer red
        else:
            # Refined green for bids - professional emerald
            odds_label.setStyleSheet("color: #34d399; font-weight: 600;")
            bar_color = "52, 211, 153"  # Softer emerald

        layout.addWidget(odds_label)
        layout.addStretch()

        # Apply refined gradient background based on liquidity
        # More liquidity = more opaque bar with smoother transitions
        opacity = min(int(bar_width * 0.35), 35)  # Max 35% opacity for subtlety
        edge_opacity = max(int(opacity * 0.6), 8)  # Softer edge
        widget.setStyleSheet(f"""
            QWidget {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba({bar_color}, {opacity}),
                    stop:{bar_width/100 * 0.7:.2f} rgba({bar_color}, {opacity}),
                    stop:{bar_width/100:.2f} rgba({bar_color}, {edge_opacity}),
                    stop:{bar_width/100:.2f} rgba(13, 15, 20, 0),
                    stop:1 rgba(13, 15, 20, 0));
                border-radius: 4px;
                border: 1px solid rgba({bar_color}, {min(opacity + 10, 45)});
            }}
        """)

        return widget

    def createSelectionItem(self, text: str, side_type: str) -> QTableWidgetItem:
        """Create selection/team name item with color coding"""
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # Use smaller font in compact mode with better font
        font_size = 8 if self.compact_mode else 13
        selection_font = QFont("SF Mono", font_size, QFont.Weight.DemiBold)
        selection_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        item.setFont(selection_font)

        if side_type == 'ask':
            item.setForeground(QColor(252, 165, 165))  # Softer red for underdogs
        else:
            item.setForeground(QColor(110, 231, 183))  # Softer green for favorites

        return item

    def createPlainItem(self, text: str) -> QTableWidgetItem:
        """Create plain text table item"""
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(QColor(209, 213, 219))  # Professional gray

        # Use smaller font in compact mode with better font
        font_size = 8 if self.compact_mode else 13
        plain_font = QFont("SF Mono", font_size, QFont.Weight.Normal)
        plain_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        item.setFont(plain_font)
        return item

    def createPercentageItem(self, text: str, percentage: float) -> QTableWidgetItem:
        """Create percentage item with color intensity based on percentage"""
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Use smaller font in compact mode with better font
        font_size = 8 if self.compact_mode else 13
        pct_font = QFont("SF Mono", font_size, QFont.Weight.DemiBold)
        pct_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        item.setFont(pct_font)

        # Refined color coding - more professional amber/gold tones
        if percentage >= 10:
            item.setForeground(QColor(251, 191, 36))  # Professional amber for large orders
        elif percentage >= 5:
            item.setForeground(QColor(252, 211, 77))  # Lighter amber for medium orders
        else:
            item.setForeground(QColor(156, 163, 175))  # Muted gray for small orders

        return item

    def formatOdds(self, odds: int) -> str:
        """Format odds for display"""
        if odds > 0:
            return f"+{odds}"
        return str(odds)


class ProphetXBrowser(QWidget):
    """
    Complete ProphetX exchange browser widget.
    Includes event search, market selection, and order book display.
    """

    # Signal emitted when user selects an event (emits event_id)
    event_selected = pyqtSignal(int)

    def __init__(self, parent=None, compact_mode=False):
        super().__init__(parent)
        self.all_events = {}
        self.filtered_events = []
        self.current_event_data = None
        self.current_event_id = None
        self.compact_mode = compact_mode
        self.initUI()

    def initUI(self):
        main_layout = QVBoxLayout(self)
        margins = 2 if self.compact_mode else 0
        main_layout.setContentsMargins(margins, margins, margins, margins)
        main_layout.setSpacing(0)

        if self.compact_mode:
            # Compact mode: only show event/market selector and orderbook
            top_panel = self.createCompactOrderBookPanel()
            main_layout.addWidget(top_panel)
        else:
            # Full mode: show everything
            # Top section: Market selector and order book
            top_panel = self.createOrderBookPanel()
            main_layout.addWidget(top_panel, 3)  # Give more space to order book

            # Bottom section: Event browser
            bottom_panel = self.createEventBrowserPanel()
            main_layout.addWidget(bottom_panel, 1)  # Give less space to event list

        # Auto-load latest data if available
        self.loadLatestData()

    def createOrderBookPanel(self):
        """Create top panel with event header, market selector and order book"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Event info header
        self.event_header = QLabel("Select an event to view markets")
        self.event_header.setFont(QFont("Arial", 13, QFont.Weight.Bold))
        self.event_header.setStyleSheet("""
            QLabel {
                background-color: #1a1d24;
                color: #ffffff;
                padding: 15px;
                border-bottom: 2px solid #4a9eff;
            }
        """)
        layout.addWidget(self.event_header)

        # Market selector
        market_row = QHBoxLayout()
        market_label = QLabel("Market:")
        market_label.setStyleSheet("color: #8a92a3; padding: 5px;")

        self.market_combo = QComboBox()
        self.market_combo.currentIndexChanged.connect(self.onMarketSelected)
        self.market_combo.setStyleSheet("""
            QComboBox {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                border-radius: 4px;
                padding: 8px;
                color: #ffffff;
                min-width: 200px;
            }
            QComboBox:hover {
                border: 1px solid #4a9eff;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                selection-background-color: #2a4a7a;
                color: #ffffff;
            }
        """)

        market_row.addWidget(market_label)
        market_row.addWidget(self.market_combo, 1)
        market_row.addStretch()

        market_container = QWidget()
        market_container.setLayout(market_row)
        market_container.setStyleSheet("background-color: #0d0f14; padding: 10px;")
        layout.addWidget(market_container)

        # Order book widget (shows all lines automatically)
        self.orderbook = OrderBookWidget()
        layout.addWidget(self.orderbook, 1)

        panel.setStyleSheet("background-color: #0d0f14;")
        return panel

    def createCompactOrderBookPanel(self):
        """Create compact panel for terminal integration - event/market selector + orderbook only"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(1)

        # Create horizontal layout for side-by-side dropdowns
        dropdown_row = QHBoxLayout()
        dropdown_row.setSpacing(4)

        # Compact event selector
        self.event_combo = QComboBox()
        self.event_combo.currentIndexChanged.connect(self.onCompactEventSelected)
        self.event_combo.setStyleSheet("""
            QComboBox {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                border-radius: 2px;
                padding: 2px 4px;
                color: #ffffff;
                font-size: 9px;
                min-height: 18px;
                max-height: 22px;
            }
            QComboBox:hover {
                border: 1px solid #4a9eff;
            }
            QComboBox::drop-down {
                width: 15px;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                selection-background-color: #2a4a7a;
                color: #ffffff;
                font-size: 9px;
            }
        """)
        dropdown_row.addWidget(self.event_combo, 1)

        # Compact market selector
        self.market_combo = QComboBox()
        self.market_combo.currentIndexChanged.connect(self.onMarketSelected)
        self.market_combo.setStyleSheet("""
            QComboBox {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                border-radius: 2px;
                padding: 2px 4px;
                color: #ffffff;
                font-size: 9px;
                min-height: 18px;
                max-height: 22px;
            }
            QComboBox:hover {
                border: 1px solid #4a9eff;
            }
            QComboBox::drop-down {
                width: 15px;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                selection-background-color: #2a4a7a;
                color: #ffffff;
                font-size: 8px;
            }
        """)
        dropdown_row.addWidget(self.market_combo, 1)

        layout.addLayout(dropdown_row)

        # Order book widget in compact mode
        self.orderbook = OrderBookWidget(compact_mode=True)
        layout.addWidget(self.orderbook, 1)

        panel.setStyleSheet("background-color: #0d0f14;")
        return panel

    def createEventBrowserPanel(self):
        """Create bottom panel with event search and list"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Title bar with search
        title_bar = QWidget()
        title_bar.setStyleSheet("background-color: #1a1d24; border-top: 2px solid #2a2d34;")
        title_layout = QHBoxLayout(title_bar)

        title = QLabel("ProphetX Events")
        title.setFont(QFont("Arial", 13, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff; padding: 8px;")
        title_layout.addWidget(title)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter events...")
        self.search_input.textChanged.connect(self.filterEvents)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: #0d0f14;
                border: 1px solid #2a2d34;
                border-radius: 4px;
                padding: 6px;
                color: #ffffff;
                max-width: 300px;
            }
            QLineEdit:focus {
                border: 1px solid #4a9eff;
            }
        """)
        title_layout.addWidget(self.search_input)

        # Stats label
        self.stats_label = QLabel("No events loaded")
        self.stats_label.setStyleSheet("color: #8a92a3; padding: 8px; font-size: 11px;")
        title_layout.addWidget(self.stats_label)
        title_layout.addStretch()

        layout.addWidget(title_bar)

        # Event list (horizontal scrolling)
        self.event_list = QListWidget()
        self.event_list.itemClicked.connect(self.onEventSelected)
        self.event_list.setStyleSheet("""
            QListWidget {
                background-color: #0d0f14;
                border: none;
                color: #ffffff;
                outline: none;
            }
            QListWidget::item {
                padding: 10px;
                border-bottom: 1px solid #1a1d24;
            }
            QListWidget::item:hover {
                background-color: #1a1d24;
            }
            QListWidget::item:selected {
                background-color: #2a4a7a;
            }
        """)
        layout.addWidget(self.event_list)

        panel.setStyleSheet("background-color: #0d0f14;")
        return panel

    def loadLatestData(self):
        """Load the most recent ProphetX data dump"""
        dump_dir = Path.cwd() / "prophetx_dumps"
        if not dump_dir.exists():
            return

        # Find latest combined data file
        combined_files = list(dump_dir.glob("all_markets_combined_*.json"))
        if not combined_files:
            return

        latest_file = max(combined_files, key=lambda p: p.stat().st_mtime)
        self.loadDataFromFile(latest_file)

    def loadDataFromFile(self, filepath: Path):
        """Load ProphetX data from JSON file"""
        try:
            with open(filepath, 'r') as f:
                self.all_events = json.load(f)

            self.populateEventList()

        except Exception as e:
            print(f"Error loading data: {e}")

    def populateEventList(self):
        """Populate event list from loaded data"""
        self.filtered_events = []

        for event_id, event_data in self.all_events.items():
            metadata = event_data.get('event_metadata', {})

            # Create display item
            event_name = metadata.get('name', 'Unknown Event')
            sport = metadata.get('sport', 'Unknown')
            stake = metadata.get('stake', 0)
            tournament = metadata.get('tournament', '')

            display_text = f"{event_name}\n{sport}"
            if tournament:
                display_text += f" • {tournament}"
            display_text += f" • ${stake:,.0f} volume"

            self.filtered_events.append({
                'id': event_id,
                'metadata': metadata,
                'data': event_data,
                'display': display_text
            })

        # Sort by stake/volume
        self.filtered_events.sort(key=lambda x: x['metadata'].get('stake', 0), reverse=True)

        # Update UI based on mode
        if self.compact_mode:
            self.refreshCompactEventCombo()
        else:
            self.refreshEventList()
            # Update stats
            total_events = len(self.filtered_events)
            total_stake = sum(e['metadata'].get('stake', 0) for e in self.filtered_events)
            self.stats_label.setText(f"{total_events} events • ${total_stake:,.0f} total volume")

    def refreshEventList(self):
        """Refresh the event list widget display"""
        self.event_list.clear()

        for event in self.filtered_events:
            item = QListWidgetItem(event['display'])
            item.setData(Qt.ItemDataRole.UserRole, event)

            # Color code by sport
            sport = event['metadata'].get('sport', '').lower()
            if 'football' in sport or 'nfl' in sport:
                item.setForeground(QColor(255, 165, 0))
            elif 'basketball' in sport or 'nba' in sport:
                item.setForeground(QColor(255, 100, 100))
            elif 'baseball' in sport or 'mlb' in sport:
                item.setForeground(QColor(100, 150, 255))
            elif 'hockey' in sport or 'nhl' in sport:
                item.setForeground(QColor(100, 200, 255))
            else:
                item.setForeground(QColor(200, 200, 200))

            self.event_list.addItem(item)

    def refreshCompactEventCombo(self):
        """Refresh the compact event combo box"""
        self.event_combo.clear()

        for event in self.filtered_events:
            metadata = event['metadata']
            event_name = metadata.get('name', 'Unknown Event')
            sport = metadata.get('sport', '')
            stake = metadata.get('stake', 0)

            # Compact display
            display = f"{event_name} (${stake:,.0f})"
            self.event_combo.addItem(display, event)

    def onCompactEventSelected(self, index: int):
        """Handle event selection in compact mode"""
        if index < 0:
            return

        event = self.event_combo.itemData(index)
        if not event:
            return

        self.current_event_data = event['data']
        metadata = event['metadata']
        self.current_event_id = metadata.get('id')

        # Emit signal for external listeners (e.g., worker to refresh data)
        if self.current_event_id:
            self.event_selected.emit(self.current_event_id)

        # Populate market selector
        self.populateMarketSelector()

    def filterEvents(self):
        """Filter events based on search text"""
        search_text = self.search_input.text().lower()

        if not search_text:
            self.populateEventList()
            return

        # Filter events
        filtered = []
        for event in self.filtered_events:
            event_name = event['metadata'].get('name', '').lower()
            sport = event['metadata'].get('sport', '').lower()
            tournament = event['metadata'].get('tournament', '').lower()

            if (search_text in event_name or
                search_text in sport or
                search_text in tournament):
                filtered.append(event)

        self.filtered_events = filtered
        self.refreshEventList()

    def onEventSelected(self, item: QListWidgetItem):
        """Handle event selection in full mode"""
        event = item.data(Qt.ItemDataRole.UserRole)
        self.current_event_data = event['data']
        metadata = event['metadata']

        # Update header
        event_name = metadata.get('name', 'Unknown Event')
        start_time = metadata.get('startTime', '')
        if start_time:
            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            time_str = dt.strftime('%b %d, %Y %I:%M %p')
            event_name += f" • {time_str}"

        self.event_header.setText(event_name)

        # Populate market selector
        self.populateMarketSelector()

    def populateMarketSelector(self):
        """Populate market selector - SORTED BY ACTIVE ORDERBOOK LIQUIDITY (highest to lowest)"""
        self.market_combo.clear()

        if not self.current_event_data:
            return

        markets = self.current_event_data.get('data', {}).get('markets', [])

        # Calculate active liquidity for each market
        markets_with_liquidity = []
        for market in markets:
            active_liquidity = self.calculateActiveMarketLiquidity(market)
            markets_with_liquidity.append((market, active_liquidity))

        # Sort by active liquidity descending
        markets_with_liquidity.sort(key=lambda x: x[1], reverse=True)

        for market, active_liquidity in markets_with_liquidity:
            market_name = market.get('name', 'Unknown')
            market_type = market.get('type', '')
            total_stake = market.get('totalStake', 0)

            if self.compact_mode:
                # Compact display - shorter format
                display = f"{market_name} (${active_liquidity:,.0f})"
            else:
                # Full display
                display = f"{market_name} (${active_liquidity:,.0f} active • ${total_stake:,.0f} total)"

            self.market_combo.addItem(display, market)

    def calculateActiveMarketLiquidity(self, market: Dict) -> float:
        """
        Calculate the total active liquidity currently on the orderbook for a market.
        This sums up all the 'value' fields from unmatched orders.

        Args:
            market: Market dictionary from ProphetX API

        Returns:
            Total active liquidity (sum of all order values)
        """
        total_active = 0.0

        # For markets with multiple lines (spread/total)
        if 'marketLines' in market and market['marketLines']:
            for market_line in market['marketLines']:
                selections = market_line.get('selections', [])
                # Each selection array is [side1_orders, side2_orders]
                for side_orders in selections:
                    if not side_orders:
                        continue
                    for order in side_orders:
                        total_active += order.get('value', 0)

        # For simple markets (moneyline)
        elif 'selections' in market:
            selections = market.get('selections', [])
            for side_orders in selections:
                if not side_orders:
                    continue
                for order in side_orders:
                    total_active += order.get('value', 0)

        return total_active

    def onMarketSelected(self, index: int):
        """Handle market selection - displays all available lines"""
        if index < 0:
            return

        market = self.market_combo.itemData(index)
        if not market:
            return

        # Display all lines from this market
        self.orderbook.setMarket(market)

    def updateEventMarkets(self, markets_data: Dict):
        """
        Update the current event with fresh markets data from async worker.

        Args:
            markets_data: Fresh markets data from ProphetXQueryAsync
        """
        if not markets_data or not self.current_event_id:
            return

        # Update the stored data
        if self.current_event_id in self.all_events:
            self.all_events[self.current_event_id] = markets_data

        # Update current event data
        self.current_event_data = markets_data

        # Refresh the market selector and orderbook display
        self.populateMarketSelector()

    def refreshData(self):
        """Refresh data from ProphetX API"""
        # Run full scrape in background
        QTimer.singleShot(0, self._refreshDataAsync)

    def _refreshDataAsync(self):
        """Async data refresh"""
        try:
            all_markets = ProphetXQuery.ScrapeAllMarkets(
                save_individual=False,
                save_combined=True
            )

            if all_markets:
                self.all_events = all_markets
                self.populateEventList()

        except Exception as e:
            print(f"Error refreshing data: {e}")


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    # Set dark theme
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(13, 15, 20))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Base, QColor(26, 29, 36))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(13, 15, 20))
    palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    app.setPalette(palette)

    # Create and show widget
    browser = ProphetXBrowser()
    browser.setWindowTitle("ProphetX Order Book Browser")
    browser.resize(1200, 800)
    browser.show()

    sys.exit(app.exec())
