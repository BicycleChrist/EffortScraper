import sys
from PyQt6.QtWidgets import (
    QApplication, QWidget, QGridLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QGroupBox, QVBoxLayout, QHBoxLayout,
    QPushButton, QScrollArea, QStackedWidget
)
from fractions import Fraction
from PyQt6.QtGui import QIcon, QFont, QPalette, QColor
from PyQt6.QtCore import Qt, QTimer
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

class MultiwayOutcome(QWidget):
    """Single outcome row in a multiway market (e.g., one team to win championship)."""
    def __init__(self, outcome_number: int, on_remove_callback=None, on_changed_callback=None):
        super().__init__()
        self.outcome_number = outcome_number
        self.on_remove_callback = on_remove_callback
        self.on_changed_callback = on_changed_callback

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 3, 5, 3)

        # Outcome name input
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(f"Outcome {outcome_number}")
        self.name_input.setFixedWidth(140)
        self.name_input.setStyleSheet("""
            QLineEdit {
                padding: 4px 6px;
                border: 1px solid #555;
                border-radius: 4px;
                background-color: rgba(255, 255, 255, 0.08);
                color: white;
            }
        """)
        layout.addWidget(self.name_input)

        # American odds input
        odds_label = QLabel("Odds:")
        odds_label.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(odds_label)

        self.american_input = QLineEdit()
        self.american_input.setPlaceholderText("+odds")
        self.american_input.setFixedWidth(80)
        self.american_input.setStyleSheet("""
            QLineEdit {
                padding: 4px 6px;
                border: 1px solid #555;
                border-radius: 4px;
                background-color: rgba(255, 255, 255, 0.08);
                color: white;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
            }
        """)
        layout.addWidget(self.american_input)

        # Implied prob (with vig)
        self.implied_label = QLabel("Imp: -")
        self.implied_label.setFixedWidth(75)
        self.implied_label.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(self.implied_label)

        # Fair prob (no vig)
        self.fair_prob_label = QLabel("Fair: -")
        self.fair_prob_label.setFixedWidth(75)
        self.fair_prob_label.setStyleSheet("color: #4CAF50; font-weight: bold; font-size: 11px;")
        layout.addWidget(self.fair_prob_label)

        # Fair American odds (no vig)
        self.fair_odds_label = QLabel("NV: -")
        self.fair_odds_label.setFixedWidth(80)
        self.fair_odds_label.setStyleSheet("color: #FFC107; font-weight: bold; font-size: 11px;")
        layout.addWidget(self.fair_odds_label)

        layout.addStretch()

        # Remove button
        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(22, 22)
        remove_btn.setStyleSheet("""
            QPushButton {
                background-color: #F44336;
                color: white;
                border-radius: 11px;
                font-weight: bold;
                font-size: 10px;
                border: none;
            }
            QPushButton:hover {
                background-color: #D32F2F;
            }
        """)
        remove_btn.clicked.connect(lambda: self.on_remove_callback(self) if self.on_remove_callback else None)
        layout.addWidget(remove_btn)

        # Connect signal
        self.american_input.textChanged.connect(self._on_odds_changed)

    def _on_odds_changed(self):
        try:
            ao = float(self.american_input.text())
            prob = implied_prob_american(ao)
            self.implied_label.setText(f"Imp: {prob*100:.1f}%")
            self.implied_label.setStyleSheet("color: #ccc; font-size: 11px;")
        except ValueError:
            self.implied_label.setText("Imp: -")
            self.implied_label.setStyleSheet("color: #aaa; font-size: 11px;")
            self.fair_prob_label.setText("Fair: -")
            self.fair_odds_label.setText("NV: -")
        if self.on_changed_callback:
            self.on_changed_callback()

    def get_implied_prob(self):
        """Return implied probability from American odds, or None."""
        try:
            ao = float(self.american_input.text())
            return implied_prob_american(ao)
        except ValueError:
            return None

    def set_fair_values(self, fair_prob):
        """Update the fair (no-vig) displays."""
        fair_odds = prob_to_american_odds(fair_prob)
        self.fair_prob_label.setText(f"Fair: {fair_prob*100:.1f}%")
        fair_text = f"+{int(fair_odds)}" if fair_odds > 0 else f"{int(fair_odds)}"
        self.fair_odds_label.setText(f"NV: {fair_text}")

    def clear_fair_values(self):
        self.fair_prob_label.setText("Fair: -")
        self.fair_odds_label.setText("NV: -")


class MultiwayDevigger(QWidget):
    """No-vig calculator for multiway markets (championship, MVP, etc.)."""
    def __init__(self):
        super().__init__()

        main_layout = QVBoxLayout(self)

        # Header
        header_label = QLabel("Multiway Market Devigger")
        header_label.setStyleSheet("""
            QLabel {
                font-size: 15px;
                font-weight: bold;
                color: #fff;
                padding: 5px;
            }
        """)
        header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(header_label)

        desc_label = QLabel("Enter American odds for each outcome to remove the vig.")
        desc_label.setStyleSheet("color: #aaa; font-size: 11px; padding-bottom: 5px;")
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(desc_label)

        # Outcomes container with scroll area
        outcomes_group = QGroupBox("Outcomes")
        outcomes_group.setStyleSheet("""
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

        outcomes_layout = QVBoxLayout()

        # Scroll area for outcomes
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: transparent;
            }
        """)

        scroll_content = QWidget()
        self.outcomes_layout = QVBoxLayout(scroll_content)
        self.outcomes_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.outcomes_layout.setSpacing(2)
        scroll.setWidget(scroll_content)

        outcomes_layout.addWidget(scroll)

        # Add outcome button
        add_btn = QPushButton("+ Add Outcome")
        add_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border-radius: 6px;
                padding: 8px;
                font-weight: bold;
                font-size: 12px;
                border: none;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        add_btn.clicked.connect(self.add_outcome)
        outcomes_layout.addWidget(add_btn)

        outcomes_group.setLayout(outcomes_layout)
        main_layout.addWidget(outcomes_group)

        # Summary / vig display
        summary_group = QGroupBox()
        summary_group.setStyleSheet("""
            QGroupBox {
                border: 2px solid #555;
                border-radius: 8px;
                margin-top: 5px;
                background-color: rgba(0, 0, 0, 0.1);
            }
        """)
        summary_layout = QHBoxLayout()
        summary_layout.setContentsMargins(10, 10, 10, 10)

        # Total implied prob
        total_prob_title = QLabel("Total Implied:")
        total_prob_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #fff;")
        self.total_prob_display = QLabel("-")
        self.total_prob_display.setStyleSheet("font-size: 14pt; font-weight: bold; color: #2196F3;")
        summary_layout.addWidget(total_prob_title)
        summary_layout.addWidget(self.total_prob_display)
        summary_layout.addStretch()

        # Vig
        vig_title = QLabel("Vigorish:")
        vig_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #fff;")
        self.vig_display = QLabel("-")
        self.vig_display.setStyleSheet("font-size: 14pt; font-weight: bold; color: #FFC107;")
        summary_layout.addWidget(vig_title)
        summary_layout.addWidget(self.vig_display)
        summary_layout.addStretch()

        # Outcome count
        count_title = QLabel("Outcomes:")
        count_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #fff;")
        self.count_display = QLabel("0")
        self.count_display.setStyleSheet("font-size: 14pt; font-weight: bold; color: #aaa;")
        summary_layout.addWidget(count_title)
        summary_layout.addWidget(self.count_display)

        summary_group.setLayout(summary_layout)
        main_layout.addWidget(summary_group)

        # Track outcomes
        self.outcomes = []
        self.outcome_counter = 0

        # Add initial 3 outcomes
        for _ in range(3):
            self.add_outcome()

    def add_outcome(self):
        self.outcome_counter += 1
        outcome = MultiwayOutcome(self.outcome_counter, self.remove_outcome, self.recalculate)
        self.outcomes.append(outcome)
        self.outcomes_layout.addWidget(outcome)
        self.recalculate()

    def remove_outcome(self, outcome):
        if len(self.outcomes) > 2:  # Keep at least 2 outcomes
            self.outcomes.remove(outcome)
            self.outcomes_layout.removeWidget(outcome)
            outcome.deleteLater()
            self.recalculate()

    def recalculate(self):
        """Recalculate fair probabilities using proportional devigging."""
        probs = []
        valid_outcomes = []

        for outcome in self.outcomes:
            p = outcome.get_implied_prob()
            if p is not None:
                probs.append(p)
                valid_outcomes.append(outcome)
            else:
                outcome.clear_fair_values()

        valid_count = len(probs)
        self.count_display.setText(f"{len(self.outcomes)} ({valid_count} valid)")

        if valid_count < 2:
            self.total_prob_display.setText("-")
            self.vig_display.setText("-")
            self.vig_display.setStyleSheet("font-size: 14pt; font-weight: bold; color: #FFC107;")
            for o in valid_outcomes:
                o.clear_fair_values()
            return

        total_prob = sum(probs)
        vig = (total_prob - 1) * 100

        self.total_prob_display.setText(f"{total_prob*100:.1f}%")

        # Vig color
        if vig < 2:
            vig_color = "#4CAF50"
        elif vig < 5:
            vig_color = "#8BC34A"
        elif vig < 10:
            vig_color = "#FFC107"
        elif vig < 20:
            vig_color = "#FF9800"
        else:
            vig_color = "#F44336"

        self.vig_display.setText(f"{vig:.1f}%")
        self.vig_display.setStyleSheet(f"font-size: 14pt; font-weight: bold; color: {vig_color};")

        # Proportional devigging: divide each probability by the total
        for outcome, prob in zip(valid_outcomes, probs):
            fair_prob = prob / total_prob
            outcome.set_fair_values(fair_prob)


class CalculatorApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Odds Calculator")

        self.icon_frame = 0
        self.icon_timer = QTimer(self)
        self.icon_timer.setSingleShot(False)
        self.icon_timer.timeout.connect(self.UpdateIcon)
        self.icon_timer.start(16)

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

        # Add all three calculators
        self.odds_calculator = OddsConverterWidget()
        self.parlay_calculator = ParlayCalculator()
        self.multiway_calculator = MultiwayDevigger()

        self.stacked_widget.addWidget(self.odds_calculator)      # index 0
        self.stacked_widget.addWidget(self.parlay_calculator)     # index 1
        self.stacked_widget.addWidget(self.multiway_calculator)   # index 2

        main_layout.addWidget(self.stacked_widget)

        # Mode definitions: (index, button_text_for_next, button_color, hover_color)
        self.modes = [
            (0, "Switch to Parlay Calculator", "#2196F3", "#1976D2"),
            (1, "Switch to Multiway Devigger", "#4CAF50", "#45a049"),
            (2, "Switch to Odds Calculator", "#9C27B0", "#7B1FA2"),
        ]
        self.current_mode = 0

    def UpdateIcon(self):
        framesdir = pathlib.Path(__file__).parent / "appicon_frames"
        next_icon = framesdir / f"frame{str(self.icon_frame).zfill(3)}.png"
        self.setWindowIcon(QIcon(str(next_icon)))
        self.icon_frame = ((self.icon_frame + 1) % 200)

    def toggle_mode(self):
        self.current_mode = (self.current_mode + 1) % len(self.modes)
        idx, btn_text, bg_color, hover_color = self.modes[self.current_mode]
        self.stacked_widget.setCurrentIndex(idx)
        self.toggle_btn.setText(btn_text)
        self.toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg_color};
                color: white;
                border-radius: 6px;
                padding: 8px 15px;
                font-weight: bold;
                font-size: 13px;
                border: none;
                min-width: 200px;
            }}
            QPushButton:hover {{
                background-color: {hover_color};
            }}
        """)

def apply_dark_palette(app):
    """Force a dark color palette so the UI looks consistent on any system."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Base, QColor(42, 42, 42))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(50, 50, 50))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Button, QColor(50, 50, 50))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(127, 127, 127))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(127, 127, 127))
    app.setPalette(palette)
    app.setStyleSheet("""
        QToolTip {
            color: #ffffff;
            background-color: #2a2a2a;
            border: 1px solid #555;
        }
    """)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    apply_dark_palette(app)
    w = CalculatorApp()
    w.show()
    sys.exit(app.exec())
