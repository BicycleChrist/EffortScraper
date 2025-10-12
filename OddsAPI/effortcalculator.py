import sys
from PyQt6.QtWidgets import (
    QApplication, QWidget, QGridLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QGroupBox, QVBoxLayout, QHBoxLayout,
    QPushButton, QScrollArea, QStackedWidget
)
from fractions import Fraction
from PyQt6.QtGui import QIcon, QFont
from PyQt6.QtCore import Qt
import pathlib

def american_to_decimal(odds):
    if odds > 0:
        return (odds + 100) / 100
    else:
        return (100 + abs(odds)) / abs(odds)

def decimal_to_american(decimal):
    if decimal >= 2:
        return (decimal - 1) * 100
    else:
        return -100 / (decimal - 1)

def decimal_to_fractional(decimal):
    frac = decimal - 1
    f = Fraction(frac).limit_denominator(100)
    return f

def implied_prob_american(odds):
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return -odds / (-odds + 100)

def devigger(prob1, prob2):
    total_prob = prob1 + prob2
    if total_prob <= 1:
        return prob1, prob2  # No vig to remove or negative vig
    return prob1 / total_prob, prob2 / total_prob

def prob_to_american_odds(prob):
    if prob > 0.5:
        return -100 * prob / (1 - prob)
    else:
        return 100 * (1 - prob) / prob

class ParlayLeg(QWidget):
    def __init__(self, leg_number: int, on_remove_callback=None):
        super().__init__()
        self.leg_number = leg_number
        self.on_remove_callback = on_remove_callback

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Leg label
        leg_label = QLabel(f"Leg {leg_number}:")
        leg_label.setStyleSheet("font-weight: bold; min-width: 60px;")
        layout.addWidget(leg_label)

        # American odds input
        self.american_input = QLineEdit()
        self.american_input.setPlaceholderText("Enter odds")
        self.american_input.setFixedWidth(100)
        layout.addWidget(self.american_input)

        # Decimal display
        self.decimal_label = QLabel("Decimal: -")
        self.decimal_label.setStyleSheet("color: #aaa;")
        layout.addWidget(self.decimal_label)

        # Implied probability display
        self.implied_label = QLabel("Prob: -")
        self.implied_label.setStyleSheet("color: #aaa;")
        layout.addWidget(self.implied_label)

        layout.addStretch()

        # Remove button
        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(25, 25)
        remove_btn.setStyleSheet("""
            QPushButton {
                background-color: #F44336;
                color: white;
                border-radius: 12px;
                font-weight: bold;
                border: none;
            }
            QPushButton:hover {
                background-color: #D32F2F;
            }
        """)
        remove_btn.clicked.connect(lambda: self.on_remove_callback(self) if self.on_remove_callback else None)
        layout.addWidget(remove_btn)

        # Connect signal
        self.american_input.textChanged.connect(self.update_display)

    def update_display(self):
        try:
            ao = float(self.american_input.text())
            dec = american_to_decimal(ao)
            prob = implied_prob_american(ao)
            self.decimal_label.setText(f"Decimal: {dec:.2f}")
            self.implied_label.setText(f"Prob: {prob*100:.1f}%")
            self.decimal_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
            self.implied_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
        except ValueError:
            self.decimal_label.setText("Decimal: -")
            self.implied_label.setText("Prob: -")
            self.decimal_label.setStyleSheet("color: #aaa;")
            self.implied_label.setStyleSheet("color: #aaa;")

    def get_decimal_odds(self):
        try:
            ao = float(self.american_input.text())
            return american_to_decimal(ao)
        except ValueError:
            return None

class ParlayCalculator(QWidget):
    def __init__(self):
        super().__init__()

        main_layout = QVBoxLayout(self)

        # Bet amount - centered and larger
        bet_layout = QHBoxLayout()
        bet_layout.addStretch()

        bet_label = QLabel("Bet Amount:")
        bet_label.setStyleSheet("""
            QLabel {
                font-size: 14px;
                font-weight: bold;
                color: #fff;
                margin-right: 10px;
            }
        """)

        self.bet_amount = QDoubleSpinBox()
        self.bet_amount.setPrefix("$")
        self.bet_amount.setValue(100)
        self.bet_amount.setMaximum(1e6)
        self.bet_amount.setStyleSheet("""
            QDoubleSpinBox {
                font-size: 14px;
                font-weight: bold;
                padding: 8px;
                border: 2px solid #555;
                border-radius: 6px;
                background-color: rgba(255, 255, 255, 0.1);
                color: white;
                min-width: 120px;
            }
            QDoubleSpinBox:focus {
                border-color: #4CAF50;
            }
        """)

        bet_layout.addWidget(bet_label)
        bet_layout.addWidget(self.bet_amount)
        bet_layout.addStretch()
        main_layout.addLayout(bet_layout)

        # Legs container with scroll area
        legs_group = QGroupBox("Parlay Legs")
        legs_group.setStyleSheet("""
            QGroupBox {
                border: 2px solid #444;
                border-radius: 8px;
                margin: 5px;
                padding-top: 20px;
                background-color: rgba(255, 255, 255, 0.05);
                font-weight: bold;
            }
            QGroupBox::title {
                color: white;
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)

        legs_layout = QVBoxLayout()

        # Scroll area for legs
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: transparent;
            }
        """)

        scroll_content = QWidget()
        self.legs_layout = QVBoxLayout(scroll_content)
        self.legs_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(scroll_content)

        legs_layout.addWidget(scroll)

        # Add leg button
        add_leg_btn = QPushButton("+ Add Leg")
        add_leg_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border-radius: 6px;
                padding: 10px;
                font-weight: bold;
                font-size: 13px;
                border: none;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        add_leg_btn.clicked.connect(self.add_leg)
        legs_layout.addWidget(add_leg_btn)

        legs_group.setLayout(legs_layout)
        main_layout.addWidget(legs_group)

        # Parlay results display
        results_group = QGroupBox()
        results_group.setStyleSheet("""
            QGroupBox {
                border: 2px solid #555;
                border-radius: 8px;
                margin-top: 10px;
                background-color: rgba(0, 0, 0, 0.1);
            }
        """)
        results_layout = QVBoxLayout()

        # Parlay odds display
        odds_layout = QHBoxLayout()
        odds_label = QLabel("Parlay Odds:")
        odds_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #fff;")
        self.parlay_odds_display = QLabel("-")
        self.parlay_odds_display.setStyleSheet("""
            font-size: 16pt;
            font-weight: bold;
            color: #4CAF50;
            padding: 5px 10px;
            background-color: rgba(76, 175, 80, 0.1);
            border-radius: 4px;
        """)
        odds_layout.addWidget(odds_label)
        odds_layout.addWidget(self.parlay_odds_display)
        odds_layout.addStretch()
        results_layout.addLayout(odds_layout)

        # Payout displays
        payout_layout = QHBoxLayout()

        to_win_label = QLabel("To Win:")
        to_win_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #fff;")
        self.to_win_display = QLabel("$0.00")
        self.to_win_display.setStyleSheet("font-size: 14pt; font-weight: bold; color: #FFC107;")

        payout_label = QLabel("Total Payout:")
        payout_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #fff;")
        self.payout_display = QLabel("$0.00")
        self.payout_display.setStyleSheet("font-size: 14pt; font-weight: bold; color: #2196F3;")

        payout_layout.addWidget(to_win_label)
        payout_layout.addWidget(self.to_win_display)
        payout_layout.addStretch()
        payout_layout.addWidget(payout_label)
        payout_layout.addWidget(self.payout_display)
        payout_layout.addStretch()
        results_layout.addLayout(payout_layout)

        results_group.setLayout(results_layout)
        main_layout.addWidget(results_group)

        # Track legs
        self.legs = []
        self.leg_counter = 0

        # Add initial 2 legs
        self.add_leg()
        self.add_leg()

        # Connect bet amount changes
        self.bet_amount.valueChanged.connect(self.calculate_parlay)

    def add_leg(self):
        self.leg_counter += 1
        leg = ParlayLeg(self.leg_counter, self.remove_leg)
        leg.american_input.textChanged.connect(self.calculate_parlay)
        self.legs.append(leg)
        self.legs_layout.addWidget(leg)
        self.calculate_parlay()

    def remove_leg(self, leg):
        if len(self.legs) > 1:  # Keep at least 1 leg
            self.legs.remove(leg)
            self.legs_layout.removeWidget(leg)
            leg.deleteLater()
            self.calculate_parlay()

    def calculate_parlay(self):
        try:
            # Get all valid decimal odds
            decimal_odds = [leg.get_decimal_odds() for leg in self.legs]
            decimal_odds = [o for o in decimal_odds if o is not None]

            if not decimal_odds:
                self.parlay_odds_display.setText("-")
                self.to_win_display.setText("$0.00")
                self.payout_display.setText("$0.00")
                return

            # Calculate parlay decimal odds (multiply all decimal odds)
            parlay_decimal = 1.0
            for odds in decimal_odds:
                parlay_decimal *= odds

            # Convert to American odds
            parlay_american = decimal_to_american(parlay_decimal)

            # Calculate payout
            bet = self.bet_amount.value()
            if parlay_american > 0:
                to_win = bet * parlay_american / 100
            else:
                to_win = bet * 100 / abs(parlay_american)

            total_payout = bet + to_win

            # Update displays
            american_text = f"+{int(parlay_american)}" if parlay_american > 0 else f"{int(parlay_american)}"
            self.parlay_odds_display.setText(f"{american_text} (Dec: {parlay_decimal:.2f})")
            self.to_win_display.setText(f"${to_win:.2f}")
            self.payout_display.setText(f"${total_payout:.2f}")

        except Exception as e:
            self.parlay_odds_display.setText("-")
            self.to_win_display.setText("$0.00")
            self.payout_display.setText("$0.00")

class OddsLine(QWidget):
    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self.layout = QGridLayout(self)

        # Title row
        title_label = QLabel(label)
        title_font = QFont()
        title_font.setBold(True)
        title_label.setFont(title_font)
        self.layout.addWidget(title_label, 0, 0, 1, 2)

        # Small info label above American field - now spans both columns
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("""
            QLabel {
                font-size: 12px;
                font-weight: bold;
                color: #2e7d32;
                padding: 4px 8px;
                background-color: rgba(46, 125, 50, 0.1);
                border-radius: 4px;
                border: 1px solid rgba(46, 125, 50, 0.3);
                text-align: center;
            }
        """)
        # Span both columns (0 and 1) to take full width
        self.layout.addWidget(self.info_label, 1, 0, 1, 2)

        # Input fields
        self.american = QLineEdit()
        self.decimal = QLineEdit()
        self.fractional = QLineEdit()
        self.implied = QLineEdit()
        self.to_win = QLineEdit()
        self.payout = QLineEdit()

        # Remove read-only from implied field
        self.to_win.setReadOnly(True)
        self.payout.setReadOnly(True)

        self.layout.addWidget(QLabel("American:"), 2, 0)
        self.layout.addWidget(self.american, 2, 1)
        self.layout.addWidget(QLabel("Decimal:"), 3, 0)
        self.layout.addWidget(self.decimal, 3, 1)
        self.layout.addWidget(QLabel("Fractional:"), 4, 0)
        self.layout.addWidget(self.fractional, 4, 1)
        self.layout.addWidget(QLabel("Implied %:"), 5, 0)
        self.layout.addWidget(self.implied, 5, 1)
        self.layout.addWidget(QLabel("To Win:"), 6, 0)
        self.layout.addWidget(self.to_win, 6, 1)
        self.layout.addWidget(QLabel("Payout:"), 7, 0)
        self.layout.addWidget(self.payout, 7, 1)

class OddsConverterWidget(QWidget):
    def __init__(self):
        super().__init__()

        main_layout = QVBoxLayout(self)

        # Bet amount - centered and larger
        bet_layout = QHBoxLayout()
        bet_layout.addStretch()

        bet_label = QLabel("Bet Amount:")
        bet_label.setStyleSheet("""
            QLabel {
                font-size: 14px;
                font-weight: bold;
                color: #fff;
                margin-right: 10px;
            }
        """)

        self.bet_amount = QDoubleSpinBox()
        self.bet_amount.setPrefix("$")
        self.bet_amount.setValue(100)
        self.bet_amount.setMaximum(1e6)
        self.bet_amount.setStyleSheet("""
            QDoubleSpinBox {
                font-size: 14px;
                font-weight: bold;
                padding: 8px;
                border: 2px solid #555;
                border-radius: 6px;
                background-color: rgba(255, 255, 255, 0.1);
                color: white;
                min-width: 120px;
            }
            QDoubleSpinBox:focus {
                border-color: #4CAF50;
            }
        """)

        bet_layout.addWidget(bet_label)
        bet_layout.addWidget(self.bet_amount)
        bet_layout.addStretch()
        main_layout.addLayout(bet_layout)

        # Lines side by side
        lines_layout = QHBoxLayout()

        self.line1 = OddsLine("Line 1")
        self.line2 = OddsLine("Line 2")

        group1 = QGroupBox()
        group1.setStyleSheet("""
            QGroupBox {
                border: 2px solid #444;
                border-radius: 8px;
                margin: 5px;
                padding-top: 10px;
                background-color: rgba(255, 255, 255, 0.05);
            }
        """)
        group1.setLayout(self.line1.layout)

        group2 = QGroupBox()
        group2.setStyleSheet("""
            QGroupBox {
                border: 2px solid #444;
                border-radius: 8px;
                margin: 5px;
                padding-top: 10px;
                background-color: rgba(255, 255, 255, 0.05);
            }
        """)
        group2.setLayout(self.line2.layout)

        lines_layout.addWidget(group1)
        lines_layout.addWidget(group2)
        main_layout.addLayout(lines_layout)

        # Vig display at bottom
        vig_container = QGroupBox()
        vig_container.setStyleSheet("""
            QGroupBox {
                border: 2px solid #555;
                border-radius: 8px;
                margin-top: 10px;
                background-color: rgba(0, 0, 0, 0.1);
            }
        """)
        vig_layout = QHBoxLayout()
        vig_layout.setContentsMargins(10, 10, 10, 10)

        vig_title = QLabel("Vigorish:")
        vig_title.setStyleSheet("""
            QLabel {
                font-size: 14px;
                font-weight: bold;
                color: #fff;
                padding: 5px;
                min-width: 80px;
            }
        """)

        # Container for the progress bar effect
        self.vig_bar_container = QWidget()
        self.vig_bar_container.setFixedHeight(40)
        self.vig_bar_container.setStyleSheet("""
            QWidget {
                background-color: rgba(255, 255, 255, 0.1);
                border-radius: 6px;
                border: 1px solid #666;
            }
        """)

        # The actual vig display label that will act as the "bar"
        self.vig_display = QLabel("")
        vig_font = QFont()
        vig_font.setPointSize(16)
        vig_font.setBold(True)
        self.vig_display.setFont(vig_font)
        self.vig_display.setStyleSheet("""
            QLabel {
                color: white;
                font-size: 16pt;
                font-weight: bold;
                padding: 8px 12px;
                border-radius: 6px;
                background-color: rgba(255, 255, 255, 0.1);
                border: none;
            }
        """)

        # Layout for the bar container
        bar_layout = QHBoxLayout(self.vig_bar_container)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.addWidget(self.vig_display)
        bar_layout.addStretch()

        vig_layout.addWidget(vig_title)
        vig_layout.addWidget(self.vig_bar_container, 1)  # Give it stretch factor
        vig_container.setLayout(vig_layout)
        main_layout.addWidget(vig_container)

        # Connect signals - add implied field to connections
        for line in [self.line1, self.line2]:
            line.american.editingFinished.connect(self.handle)
            line.decimal.editingFinished.connect(self.handle)
            line.fractional.editingFinished.connect(self.handle)
            line.implied.editingFinished.connect(self.handle)

        self.bet_amount.valueChanged.connect(self.handle)

    def get_vig_color(self, vig_percent):
        if vig_percent < 2:
            return "#4CAF50", "white"  # Green background, white text
        elif vig_percent < 5:
            return "#FFC107", "black"  # Yellow background, black text
        elif vig_percent < 8:
            return "#FF9800", "white"  # Orange background, white text
        else:
            return "#F44336", "white"  # Red background, white text

    def handle(self):
        sender = self.sender()

        for line in [self.line1, self.line2]:
            ao = None

            # Determine which field was edited for this line
            if sender == line.american:
                # American field was edited - try to parse it first
                try:
                    ao = float(line.american.text())
                except ValueError:
                    continue
            elif sender == line.decimal:
                # Decimal field was edited
                try:
                    dec = float(line.decimal.text())
                    ao = decimal_to_american(dec)
                except ValueError:
                    continue
            elif sender == line.fractional:
                # Fractional field was edited
                try:
                    num, den = map(int, line.fractional.text().split('/'))
                    dec = num / den + 1
                    ao = decimal_to_american(dec)
                except ValueError:
                    continue
            elif sender == line.implied:
                # Implied probability field was edited
                try:
                    prob = float(line.implied.text().strip('%')) / 100
                    ao = prob_to_american_odds(prob)
                except ValueError:
                    continue
            else:
                # No field was edited (bet amount changed), use existing parsing logic
                try:
                    ao = float(line.american.text())
                except ValueError:
                    try:
                        dec = float(line.decimal.text())
                        ao = decimal_to_american(dec)
                    except ValueError:
                        try:
                            num, den = map(int, line.fractional.text().split('/'))
                            dec = num / den + 1
                            ao = decimal_to_american(dec)
                        except ValueError:
                            try:
                                prob = float(line.implied.text().strip('%')) / 100
                                ao = prob_to_american_odds(prob)
                            except:
                                continue

            if ao is None:
                continue

            # Update all fields based on the parsed American odds
            dec = american_to_decimal(ao)
            line.decimal.setText(f"{dec:.2f}")
            frac = decimal_to_fractional(dec)
            line.fractional.setText(f"{frac.numerator}/{frac.denominator}")
            prob = implied_prob_american(ao)
            line.implied.setText(f"{prob*100:.2f}%")
            line.american.setText(f"{int(ao)}")

            amt = self.bet_amount.value()
            if ao > 0:
                win = amt * ao / 100
            else:
                win = amt * 100 / abs(ao)
            line.to_win.setText(f"${win:.2f}")
            line.payout.setText(f"${amt + win:.2f}")

        self.update_vig_and_fair_odds()

    def update_vig_and_fair_odds(self):
        try:
            prob1 = float(self.line1.implied.text().strip('%')) / 100
            prob2 = float(self.line2.implied.text().strip('%')) / 100
            vig = (prob1 + prob2 - 1) * 100

            # Calculate fair odds
            devigged_prob1, devigged_prob2 = devigger(prob1, prob2)
            fair_odds1 = prob_to_american_odds(devigged_prob1)
            fair_odds2 = prob_to_american_odds(devigged_prob2)

            # Calculate implied probabilities of the fair odds
            fair_prob1 = implied_prob_american(fair_odds1)
            fair_prob2 = implied_prob_american(fair_odds2)

            # Calculate progress bar width (0% = 0 width, 15% = 100% width)
            max_vig = 15.0
            vig_percentage = max(0, min(abs(vig), max_vig)) / max_vig  # Normalize to 0-1

            # Get color based on vig level - smooth gradient
            if vig_percentage <= 0.133:  # 0-2%
                color = "#4CAF50"  # Green
            elif vig_percentage <= 0.333:  # 2-5%
                color = "#8BC34A"  # Light green
            elif vig_percentage <= 0.533:  # 5-8%
                color = "#FFC107"  # Yellow
            elif vig_percentage <= 0.733:  # 8-11%
                color = "#FF9800"  # Orange
            else:  # 11%+
                color = "#F44336"  # Red

            # Calculate the width as percentage of container
            bar_width_percent = int(vig_percentage * 100)

            # Update the vig display with progress bar effect
            self.vig_display.setText(f"{vig:.1f}%")

            # Create gradient effect by adjusting the width
            self.vig_bar_container.setStyleSheet(f"""
                QWidget {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 {color},
                        stop:{vig_percentage:.3f} {color},
                        stop:{vig_percentage:.3f} rgba(255, 255, 255, 0.1),
                        stop:1 rgba(255, 255, 255, 0.1));
                    border-radius: 6px;
                    border: 1px solid #666;
                }}
            """)

            self.vig_display.setStyleSheet(f"""
                QLabel {{
                    color: white;
                    font-size: 16pt;
                    font-weight: bold;
                    padding: 8px 12px;
                    border-radius: 6px;
                    background-color: transparent;
                    border: none;
                }}
            """)

            # Update Line 1 - show fair odds and implied probability
            fair1_text = f"{int(fair_odds1):+d}" if fair_odds1 > 0 else f"{int(fair_odds1)}"
            self.line1.info_label.setText(f"NV Fair Odds: {fair1_text}  •  NV Imp Prob: {fair_prob1*100:.1f}%")

            # Update Line 2 - show fair odds and implied probability
            fair2_text = f"{int(fair_odds2):+d}" if fair_odds2 > 0 else f"{int(fair_odds2)}"
            self.line2.info_label.setText(f"NV Fair Odds: {fair2_text}  •  NV Imp Prob: {fair_prob2*100:.1f}%")

        except:
            # Clear labels if calculation fails
            self.vig_display.clear()
            self.vig_bar_container.setStyleSheet("""
                QWidget {
                    background-color: rgba(255, 255, 255, 0.1);
                    border-radius: 6px;
                    border: 1px solid #666;
                }
            """)
            self.line1.info_label.clear()
            self.line2.info_label.clear()

class CalculatorApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Odds Calculator")

        icon_path = pathlib.Path(__file__).parent / "AppIcon.png"
        self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(550, 450)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Top bar with toggle button
        top_bar = QWidget()
        top_bar.setStyleSheet("""
            QWidget {
                background-color: rgba(0, 0, 0, 0.2);
                border-bottom: 2px solid #444;
            }
        """)
        top_bar.setFixedHeight(50)

        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 5, 10, 5)

        # Toggle button
        self.toggle_btn = QPushButton("Switch to Parlay Calculator")
        self.toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border-radius: 6px;
                padding: 8px 15px;
                font-weight: bold;
                font-size: 13px;
                border: none;
                min-width: 200px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
        """)
        self.toggle_btn.clicked.connect(self.toggle_mode)
        top_layout.addWidget(self.toggle_btn)
        top_layout.addStretch()

        main_layout.addWidget(top_bar)

        # Stacked widget for switching between calculators
        self.stacked_widget = QStackedWidget()

        # Add both calculators
        self.odds_calculator = OddsConverterWidget()
        self.parlay_calculator = ParlayCalculator()

        self.stacked_widget.addWidget(self.odds_calculator)
        self.stacked_widget.addWidget(self.parlay_calculator)

        main_layout.addWidget(self.stacked_widget)

        # Current mode (0 = odds, 1 = parlay)
        self.current_mode = 0

    def toggle_mode(self):
        if self.current_mode == 0:
            # Switch to parlay
            self.current_mode = 1
            self.stacked_widget.setCurrentIndex(1)
            self.toggle_btn.setText("Switch to Odds Calculator")
            self.toggle_btn.setStyleSheet("""
                QPushButton {
                    background-color: #4CAF50;
                    color: white;
                    border-radius: 6px;
                    padding: 8px 15px;
                    font-weight: bold;
                    font-size: 13px;
                    border: none;
                    min-width: 200px;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
            """)
        else:
            # Switch to odds
            self.current_mode = 0
            self.stacked_widget.setCurrentIndex(0)
            self.toggle_btn.setText("Switch to Parlay Calculator")
            self.toggle_btn.setStyleSheet("""
                QPushButton {
                    background-color: #2196F3;
                    color: white;
                    border-radius: 6px;
                    padding: 8px 15px;
                    font-weight: bold;
                    font-size: 13px;
                    border: none;
                    min-width: 200px;
                }
                QPushButton:hover {
                    background-color: #1976D2;
                }
            """)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    w = CalculatorApp()
    w.show()
    sys.exit(app.exec())
