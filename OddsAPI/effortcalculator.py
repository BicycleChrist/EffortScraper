import sys
import math
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QGridLayout, QLabel,
    QDoubleSpinBox, QSpinBox, QLineEdit, QGroupBox, QVBoxLayout, QHBoxLayout,
    QPushButton, QScrollArea, QStackedWidget, QComboBox, QCheckBox, QSlider,
    QSizePolicy
)
from fractions import Fraction
from PyQt6.QtGui import QIcon, QFont, QPalette, QColor
from PyQt6.QtCore import Qt, QTimer
import pathlib

import pyqtgraph as pg

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

        # Outcome number label
        num_label = QLabel(f"#{outcome_number}")
        num_label.setFixedWidth(30)
        num_label.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(num_label)

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


def abbreviate(value, prefix=""):
    """1234567 -> '1.23M'. Keeps 3 significant figures inside each magnitude."""
    def trim(text):
        # only a fraction has trailing zeros to lose: "500" must stay "500"
        return text.rstrip("0").rstrip(".") if "." in text else text

    sign = "-" if value < 0 else ""
    v = abs(value)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        # 0.9995 so a value that rounds up to the next magnitude is promoted
        # into it — 999,999 reads $1M, not $1000K (or, once trimmed, $1K)
        if v >= divisor * 0.9995:
            scaled = v / divisor
            # 1.23M / 12.3M / 123M — never more than 4 characters of number
            digits = 2 if scaled < 10 else (1 if scaled < 100 else 0)
            return f"{sign}{prefix}{trim(f'{scaled:.{digits}f}')}{suffix}"
    if v >= 10 or v == 0:
        return f"{sign}{prefix}{v:,.0f}"
    if v >= 1:
        return f"{sign}{prefix}{trim(f'{v:.2f}')}"
    return f"{sign}{prefix}{v:.2f}"


def money(value):
    """Exact dollars while they still fit a banner tile, abbreviated beyond."""
    if abs(value) < 1e6:
        return f"${value:,.2f}"
    return abbreviate(value, "$")


class AbbreviatedAxis(pg.AxisItem):
    """Axis that labels ticks as $1.5K / $2M instead of 1e6 or 2·10⁶.

    Handles log mode itself — there the tick values pyqtgraph hands over are
    exponents, and the default renders them as superscript powers of ten.
    """

    def __init__(self, *args, prefix="", **kwargs):
        super().__init__(*args, **kwargs)
        self.prefix = prefix
        # our own formatting supplies the magnitude, so keep pyqtgraph from
        # also factoring one out into the axis label
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            value = 10 ** v if self.logMode else v * scale
            out.append(abbreviate(value, self.prefix))
        return out


def _btn_style(bg, hover):
    return f"""
        QPushButton {{
            background-color: {bg};
            color: white;
            border-radius: 6px;
            padding: 7px 12px;
            font-weight: bold;
            font-size: 12px;
            border: none;
        }}
        QPushButton:hover {{
            background-color: {hover};
        }}
        QPushButton:disabled {{
            background-color: #3a3a3a;
            color: #777;
        }}
    """


class KellyCalculator(QWidget):
    """Kelly stake sizing plus an animated bankroll simulation.

    The user supplies a bankroll, their true win probability, the offered
    (American) price and how much of full Kelly to bet. Running the sim places
    one bet per tick and streams the bankroll path onto a live plot until the
    bankroll trips the ruin threshold, hits the bet cap, or the user stops it.
    """

    # a compounding path is stopped at this multiple of the starting bankroll
    RUNAWAY = 1e12

    # (label, prefix, suffix, min, max, step, decimals, default, tooltip)
    SIZING_MODES = [
        ("Fractional Kelly", "", " x", 0.01, 3.0, 0.05, 2, 0.5,
         "Multiple of full Kelly, applied to the current bankroll.\n"
         "0.5 = half Kelly, 1.0 = full Kelly, >1 = overbetting."),
        ("% of bankroll", "", " %", 0.01, 100.0, 0.25, 2, 2.0,
         "Wager this % of the CURRENT bankroll on every bet — recomputed\n"
         "as the bankroll moves, so the stake shrinks on the way down."),
        ("Flat stake", "$ ", "", 0.01, 10_000_000.0, 10.0, 2, 50.0,
         "The same dollar amount on every bet, regardless of bankroll."),
    ]

    def __init__(self):
        super().__init__()

        # --- simulation state ---
        self.running = False
        self.bets = 0
        self.wins = 0
        self.losses = 0
        self.bankroll = 0.0
        self.start_bankroll = 0.0
        self.peak = 0.0
        self.max_drawdown = 0.0
        self.xs = []
        self.ys = []
        self.history_curves = []
        self._sim_p = 0.0
        self._sim_b = 0.0
        self._sim_g = 0.0
        self._sim_edge = 0.0
        self._sizing_mode = 0
        self._y_view = None  # y-range we last set, for follow hysteresis

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.step)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(4)

        # ------------------------------------------------------------------
        # Banner — every readout lives here so the chart owns the rest
        # ------------------------------------------------------------------
        banner = QWidget()
        banner.setStyleSheet("""
            QWidget#kellyBanner {
                border: 1px solid #555;
                border-radius: 8px;
                background-color: rgba(0, 0, 0, 0.25);
            }
        """)
        banner.setObjectName("kellyBanner")
        banner_grid = QGridLayout(banner)
        banner_grid.setContentsMargins(10, 5, 10, 5)
        banner_grid.setHorizontalSpacing(10)
        banner_grid.setVerticalSpacing(0)

        self.cur_display = self._tile(banner_grid, 0, 0, "BANKROLL", "#4CAF50")
        self.bets_display = self._tile(banner_grid, 0, 1, "BETS", "#ddd")
        self.record_display = self._tile(banner_grid, 0, 2, "W-L", "#2196F3")
        self.peak_display = self._tile(banner_grid, 0, 3, "PEAK", "#8BC34A")
        self.dd_display = self._tile(banner_grid, 0, 4, "MAX DD", "#FF9800")

        self.edge_display = self._tile(banner_grid, 1, 0, "EDGE / UNIT", "#4CAF50")
        self.fullkelly_display = self._tile(banner_grid, 1, 1, "FULL KELLY", "#2196F3")
        self.stake_display = self._tile(banner_grid, 1, 2, "NEXT STAKE", "#FFC107")
        self.growth_display = self._tile(banner_grid, 1, 3, "LOG GROWTH / BET", "#CE93D8")
        self.status_display = self._tile(banner_grid, 1, 4, "STATUS", "#aaa", big=False)

        for col in range(5):
            banner_grid.setColumnStretch(col, 1)
        main_layout.addWidget(banner)

        # ------------------------------------------------------------------
        # Inputs — one row
        # ------------------------------------------------------------------
        input_row = QHBoxLayout()
        input_row.setContentsMargins(4, 0, 4, 0)
        input_row.setSpacing(6)

        def _lbl(text, tip=None):
            lab = QLabel(text)
            lab.setStyleSheet("font-size: 11px; color: #ddd;")
            if tip:
                lab.setToolTip(tip)
            return lab

        self.bankroll_spin = QDoubleSpinBox()
        self.bankroll_spin.setRange(1.0, 100_000_000.0)
        self.bankroll_spin.setDecimals(2)
        self.bankroll_spin.setSingleStep(100.0)
        self.bankroll_spin.setValue(1000.0)
        self.bankroll_spin.setPrefix("$ ")
        self.bankroll_spin.setToolTip("Starting bankroll")

        self.prob_spin = QDoubleSpinBox()
        self.prob_spin.setRange(0.1, 99.9)
        self.prob_spin.setDecimals(2)
        self.prob_spin.setSuffix(" %")
        self.prob_spin.setValue(55.0)
        self.prob_spin.setToolTip("Your true win probability for the bet")

        self.odds_spin = QDoubleSpinBox()
        self.odds_spin.setRange(-100000.0, 100000.0)
        self.odds_spin.setDecimals(0)
        self.odds_spin.setSingleStep(5.0)
        self.odds_spin.setValue(-110.0)
        self.odds_spin.setToolTip("Offered price in American odds")

        # Sizing: one combo picks the rule, one spin holds that rule's number.
        # SIZING_MODES entries are
        # (label, prefix, suffix, min, max, step, decimals, default, tooltip).
        self.sizing_combo = QComboBox()
        self.sizing_combo.addItems([m[0] for m in self.SIZING_MODES])
        self.sizing_combo.setToolTip("How each stake is sized")
        self.sizing_combo.setMinimumWidth(105)

        self.size_spin = QDoubleSpinBox()
        # remembered value per mode, so flipping the combo does not lose an entry
        self._size_values = [m[7] for m in self.SIZING_MODES]

        self.ruin_spin = QDoubleSpinBox()
        self.ruin_spin.setRange(0.0, 99.0)
        self.ruin_spin.setDecimals(2)
        self.ruin_spin.setSuffix(" %")
        self.ruin_spin.setValue(1.0)
        self.ruin_spin.setToolTip(
            "Stop when the bankroll falls to this % of its starting value.\n"
            "Proportional Kelly never reaches exactly $0, so ruin needs a floor."
        )

        self.maxbets_spin = QSpinBox()
        self.maxbets_spin.setRange(0, 10_000_000)
        self.maxbets_spin.setSingleStep(100)
        self.maxbets_spin.setValue(0)
        self.maxbets_spin.setSpecialValueText("unlimited")
        self.maxbets_spin.setToolTip("Stop after this many bets (0 = run until ruin or stopped)")

        for w in (self.bankroll_spin, self.prob_spin, self.odds_spin, self.size_spin,
                  self.ruin_spin, self.maxbets_spin, self.sizing_combo):
            w.setStyleSheet("font-size: 11px; padding: 2px;")
            # Preferred+small floor: the row squeezes on a narrow window rather
            # than pinning a minimum width under the whole (shared) window
            w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            w.setMinimumWidth(56)

        for label, widget, stretch in (
            ("Bankroll", self.bankroll_spin, 3),
            ("Win prob", self.prob_spin, 2),
            ("Odds", self.odds_spin, 2),
            ("Sizing", self.sizing_combo, 3),
            ("", self.size_spin, 2),
        ):
            if label:
                input_row.addWidget(_lbl(label))
            input_row.addWidget(widget, stretch)

        main_layout.addLayout(input_row)

        # ------------------------------------------------------------------
        # Controls
        # ------------------------------------------------------------------
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(4, 0, 4, 0)
        ctrl_row.setSpacing(6)

        self.run_btn = QPushButton("▶  Run")
        self.run_btn.setStyleSheet(_btn_style("#4CAF50", "#45a049"))
        self.run_btn.clicked.connect(self.toggle_run)
        ctrl_row.addWidget(self.run_btn)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setStyleSheet(_btn_style("#607D8B", "#4c626c"))
        self.reset_btn.clicked.connect(self.reset)
        ctrl_row.addWidget(self.reset_btn)

        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(1, 100)
        self.speed_slider.setValue(30)
        self.speed_slider.setMinimumWidth(55)
        self.speed_slider.setMaximumWidth(95)
        self.speed_slider.setToolTip("Speed — bets per second (high values batch bets per frame)")
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        ctrl_row.addWidget(self.speed_slider)

        self.speed_label = QLabel()
        self.speed_label.setStyleSheet("font-size: 11px; color: #aaa; min-width: 46px;")
        ctrl_row.addWidget(self.speed_label)

        ctrl_row.addWidget(_lbl("View", "Bets kept in view while following (0 = fit the whole path)"))
        self.window_spin = QSpinBox()
        self.window_spin.setRange(0, 1_000_000)
        self.window_spin.setSingleStep(100)
        self.window_spin.setValue(400)
        self.window_spin.setSpecialValueText("fit all")
        self.window_spin.setMaximumWidth(80)
        self.window_spin.setMinimumWidth(56)
        self.window_spin.setToolTip("Bets kept in view while following (0 = fit the whole path)")
        self.window_spin.valueChanged.connect(lambda _: self._follow_viewport(force=True))
        ctrl_row.addWidget(self.window_spin)

        ctrl_row.addWidget(_lbl("Ruin", "Stop when the bankroll falls to this % of its starting value"))
        ctrl_row.addWidget(self.ruin_spin)
        ctrl_row.addWidget(_lbl("Cap", "Stop after this many bets (0 = run until ruin or stopped)"))
        ctrl_row.addWidget(self.maxbets_spin)

        self.follow_check = QCheckBox("Follow")
        self.follow_check.setStyleSheet("font-size: 11px; color: #ddd;")
        self.follow_check.setChecked(True)
        self.follow_check.setToolTip(
            "Scroll the view to keep the live bet in frame.\n"
            "Panning or zooming the chart by hand turns this off."
        )
        self.follow_check.toggled.connect(self._on_follow_toggled)
        ctrl_row.addWidget(self.follow_check)

        self.log_check = QCheckBox("Log")
        self.log_check.setStyleSheet("font-size: 11px; color: #ddd;")
        self.log_check.setChecked(True)
        self.log_check.setToolTip("Log-scale the bankroll axis — equal % moves get equal height")
        self.log_check.toggled.connect(self._on_log_toggled)
        ctrl_row.addWidget(self.log_check)

        self.keep_check = QCheckBox("Keep")
        self.keep_check.setStyleSheet("font-size: 11px; color: #ddd;")
        self.keep_check.setChecked(True)
        self.keep_check.setToolTip("Leave finished paths on the chart as faded curves")
        ctrl_row.addWidget(self.keep_check)

        ctrl_row.addStretch()
        main_layout.addLayout(ctrl_row)

        # ------------------------------------------------------------------
        # Plot
        # ------------------------------------------------------------------
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background="#1b1b1b", axisItems={
            "left": AbbreviatedAxis(orientation="left", prefix="$"),
            "bottom": AbbreviatedAxis(orientation="bottom"),
        })
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "Bets placed", color="#aaa")
        self.plot.setLabel("left", "Bankroll", color="#aaa")
        self.plot.setMinimumHeight(240)
        self.plot.setLogMode(False, True)
        # long runs reach 6-figure point counts; peak-downsample for the redraw
        # (never setClipToView — it crashes under Python 3.14)
        self.plot.setDownsampling(auto=True, mode="peak")
        legend = self.plot.addLegend(offset=(-10, 10), labelTextColor="#999",
                                     horSpacing=14, verSpacing=-4)
        legend.setBrush(pg.mkBrush(0, 0, 0, 0))
        legend.setPen(pg.mkPen(None))
        legend.setLabelTextSize("7pt")

        self.expected_curve = self.plot.plot(
            [], [], pen=pg.mkPen("#9C27B0", width=1, style=Qt.PenStyle.DashLine),
            name="Expected growth"
        )
        self.bankroll_curve = self.plot.plot(
            [], [], pen=pg.mkPen("#4CAF50", width=2), name="Bankroll"
        )
        self.start_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen("#888", width=1, style=Qt.PenStyle.DotLine)
        )
        self.ruin_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen("#F44336", width=1, style=Qt.PenStyle.DashLine)
        )
        self.plot.addItem(self.start_line, ignoreBounds=True)
        self.plot.addItem(self.ruin_line, ignoreBounds=True)

        # crosshair readout
        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen("#666", width=1))
        self.hline = pg.InfiniteLine(angle=0, movable=False,
                                     pen=pg.mkPen("#666", width=1))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.addItem(self.hline, ignoreBounds=True)
        self.cursor_text = pg.TextItem(color="#ddd", anchor=(0, 1))
        self.plot.addItem(self.cursor_text, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # a hand pan/zoom means the user wants control — stop fighting them
        self.plot.getPlotItem().vb.sigRangeChangedManually.connect(
            self._on_range_changed_manually
        )

        main_layout.addWidget(self.plot, stretch=1)

        # wiring
        self.sizing_combo.currentIndexChanged.connect(self._on_sizing_changed)
        self.size_spin.valueChanged.connect(self._on_size_value_changed)
        for w in (self.bankroll_spin, self.prob_spin, self.odds_spin, self.ruin_spin):
            w.valueChanged.connect(self.recalculate)

        self._on_speed_changed(self.speed_slider.value())
        self._on_sizing_changed(0)
        self.reset()

    # ----------------------------------------------------------------------
    # helpers
    # ----------------------------------------------------------------------
    def _tile(self, grid, row, col, title, color, big=True):
        """A banner cell: small caps caption over a coloured value."""
        cell = QVBoxLayout()
        cell.setSpacing(0)
        cell.setContentsMargins(0, 2, 0, 2)
        cap = QLabel(title)
        cap.setStyleSheet("font-size: 8px; font-weight: bold; color: #888;")
        val = QLabel("-")
        val.setStyleSheet(
            f"font-size: {'12pt' if big else '9pt'}; font-weight: bold; color: {color};"
        )
        val.setWordWrap(not big)
        cell.addWidget(cap)
        cell.addWidget(val)
        grid.addLayout(cell, row, col)
        return val

    def _american_to_b(self):
        """Net decimal profit per unit staked (decimal odds - 1)."""
        odds = self.odds_spin.value()
        if -100 < odds < 100:
            # -99..+99 are not valid American prices; treat as even money
            return 1.0
        return american_to_decimal(odds) - 1.0

    def _full_kelly(self):
        p = self.prob_spin.value() / 100.0
        b = self._american_to_b()
        if b <= 0:
            return 0.0, p, b
        return (p * b - (1.0 - p)) / b, p, b

    def _stake_for(self, bankroll):
        mode = self.sizing_combo.currentIndex()
        value = self.size_spin.value()
        if mode == 0:
            f_full, _, _ = self._full_kelly()
            stake = max(0.0, f_full) * value * bankroll
        elif mode == 1:
            stake = bankroll * value / 100.0
        else:
            stake = value
        return max(0.0, min(stake, bankroll))

    # ----------------------------------------------------------------------
    # readout / reset
    # ----------------------------------------------------------------------
    def recalculate(self):
        f_full, p, b = self._full_kelly()
        edge = p * b - (1.0 - p)  # EV per unit staked
        self._sim_edge = edge

        self.edge_display.setText(f"{edge*100:+.2f}%")
        self.edge_display.setStyleSheet(
            "font-size: 12pt; font-weight: bold; color: "
            + ("#4CAF50" if edge > 0 else "#F44336") + ";"
        )
        self.fullkelly_display.setText(f"{f_full*100:.2f}%" if f_full > 0 else "none")

        bankroll = self.bankroll if self.bets else self.bankroll_spin.value()
        stake = self._stake_for(bankroll)
        pct = (stake / bankroll * 100.0) if bankroll > 0 else 0.0
        self.stake_display.setText(f"{money(stake)} ({pct:.2f}%)")
        self.stake_display.setToolTip(f"${stake:,.2f} of a ${bankroll:,.2f} bankroll")

        # expected log growth per bet at the stake fraction actually used
        f = (stake / bankroll) if bankroll > 0 else 0.0
        if 0 < f < 1 and b > 0:
            g = p * math.log(1 + f * b) + (1 - p) * math.log(1 - f)
        else:
            g = 0.0
        self._sim_g = g
        self.growth_display.setText(f"{g*100:+.3f}%")
        self.growth_display.setStyleSheet(
            "font-size: 12pt; font-weight: bold; color: "
            + ("#CE93D8" if g > 0 else "#F44336") + ";"
        )

        if not self.running and not self.bets:
            self.start_line.setValue(self._y(self.bankroll_spin.value()))
            self.ruin_line.setValue(self._y(self._ruin_level(self.bankroll_spin.value())))

    def _ruin_level(self, start):
        return max(1e-9, start * self.ruin_spin.value() / 100.0)

    def _y(self, value):
        """Map a bankroll value into *view* y-space.

        PlotDataItem log-transforms its own data when the plot is in log mode,
        so curves get raw dollars — but InfiniteLine/TextItem positions are
        view coordinates and must be transformed by hand.
        """
        if self.log_check.isChecked():
            return math.log10(max(value, 1e-9))
        return value

    def reset(self):
        self.timer.stop()
        self.running = False
        self.run_btn.setText("▶  Run")
        self.run_btn.setStyleSheet(_btn_style("#4CAF50", "#45a049"))

        if self.keep_check.isChecked() and len(self.xs) > 1:
            ghost = self.plot.plot(
                list(self.xs), list(self.ys),
                pen=pg.mkPen(100, 100, 100, 90, width=1)
            )
            ghost.setZValue(-10)
            self.history_curves.append(ghost)
        else:
            for curve in self.history_curves:
                self.plot.removeItem(curve)
            self.history_curves.clear()

        self.start_bankroll = self.bankroll_spin.value()
        self.bankroll = self.start_bankroll
        self.peak = self.start_bankroll
        self.max_drawdown = 0.0
        self.bets = 0
        self.wins = 0
        self.losses = 0
        self.xs = [0]
        self.ys = [self.bankroll]

        self.bankroll_curve.setData([0], [self.bankroll])
        self.expected_curve.setData([], [])
        self.start_line.setValue(self._y(self.start_bankroll))
        self.ruin_line.setValue(self._y(self._ruin_level(self.start_bankroll)))
        self._set_status("Ready")
        self.update_stats()
        self.recalculate()
        self._y_view = None
        if self.follow_check.isChecked():
            self._follow_viewport(force=True)
        else:
            self.plot.enableAutoRange()

    # ----------------------------------------------------------------------
    # run loop
    # ----------------------------------------------------------------------
    def toggle_run(self):
        if self.running:
            self.stop("Paused")
            return

        f_full, p, b = self._full_kelly()
        if b <= 0:
            self._set_status("Bad odds", "That price has no payout to bet into.")
            return
        if self.sizing_combo.currentIndex() == 0 and f_full <= 0:
            self._set_status(
                "No edge",
                "Full Kelly is zero or negative at this price, so there is "
                "nothing to size. Raise the win probability, take a longer "
                "price, or switch the sizing rule."
            )
            return
        if self.bankroll <= self._ruin_level(self.start_bankroll):
            self.reset()

        # freeze the parameters the path is generated from
        self._sim_p = p
        self._sim_b = b
        self.running = True
        self.run_btn.setText("■  Stop")
        self.run_btn.setStyleSheet(_btn_style("#F44336", "#d32f2f"))
        self._set_status("Running…")
        self.timer.start(self._interval_ms)

    def _set_status(self, text, detail=None):
        """Status tile is narrow — long explanations go in the tooltip."""
        self.status_display.setText(text)
        self.status_display.setToolTip(detail or text)

    def stop(self, message=""):
        self.timer.stop()
        self.running = False
        self.run_btn.setText("▶  Run")
        self.run_btn.setStyleSheet(_btn_style("#4CAF50", "#45a049"))
        if message:
            self._set_status(message)

    def step(self):
        """Place `self._bets_per_tick` bets, then redraw once."""
        p, b = self._sim_p, self._sim_b
        ruin = self._ruin_level(self.start_bankroll)
        cap = self.maxbets_spin.value()
        finished = None

        for _ in range(self._bets_per_tick):
            stake = self._stake_for(self.bankroll)
            if stake <= 0:
                finished = ("Stake is zero",
                            "The bankroll can no longer fund a bet at this sizing rule.")
                break

            if random.random() < p:
                self.bankroll += stake * b
                self.wins += 1
            else:
                self.bankroll -= stake
                self.losses += 1
            self.bets += 1

            self.xs.append(self.bets)
            self.ys.append(self.bankroll)

            if self.bankroll > self.peak:
                self.peak = self.bankroll
            dd = (self.peak - self.bankroll) / self.peak if self.peak > 0 else 0.0
            if dd > self.max_drawdown:
                self.max_drawdown = dd

            if self.bankroll <= ruin:
                finished = (f"RUIN at bet {self.bets:,}",
                            f"Bankroll fell to ${self.bankroll:,.2f} — at or below the "
                            f"ruin floor of ${ruin:,.2f} — after {self.bets:,} bets.")
                break
            if self.bankroll >= self.RUNAWAY * self.start_bankroll:
                # a positive-edge Kelly path compounds without bound; stop
                # before the float overflows to inf and poisons the stats
                finished = (f"Ran away ({self.bets:,} bets)",
                            f"Bankroll compounded past {self.RUNAWAY:.0e}x its starting "
                            f"value, which would overflow the float — stopped.")
                break
            if cap and self.bets >= cap:
                finished = (f"Hit {cap:,}-bet cap",
                            f"Reached the bet cap at ${self.bankroll:,.2f}.")
                break

        self.redraw()
        self.update_stats()
        if finished:
            self.stop()
            self._set_status(*finished)

    def _expected_at(self, i):
        """Reference bankroll after `i` bets, or None if there is no reference.

        Flat staking has a linear expectation; proportional staking compounds
        at the expected log-growth rate, which is the typical (median) path
        rather than the mean.
        """
        if self.sizing_combo.currentIndex() == 2:
            return self.start_bankroll + self._sim_edge * self.size_spin.value() * i
        if self._sim_g:
            return self.start_bankroll * math.exp(self._sim_g * i)
        return None

    def redraw(self):
        self.bankroll_curve.setData(self.xs, self.ys)
        if not self.bets:
            self.expected_curve.setData([], [])
            self._follow_viewport()
            return

        n = self.bets
        step = max(1, n // 200)
        ex_x = list(range(0, n + 1, step))
        if ex_x[-1] != n:
            ex_x.append(n)

        if self._expected_at(0) is None:
            self.expected_curve.setData([], [])
        else:
            self.expected_curve.setData(ex_x, [self._expected_at(i) for i in ex_x])

        self._follow_viewport()

    def update_stats(self):
        self.cur_display.setText(money(self.bankroll))
        ret = (self.bankroll / self.start_bankroll - 1.0) * 100 if self.start_bankroll else 0.0
        self.cur_display.setStyleSheet(
            "font-size: 12pt; font-weight: bold; color: "
            + ("#4CAF50" if ret >= 0 else "#F44336") + ";"
        )
        self.cur_display.setToolTip(f"${self.bankroll:,.2f}   ({ret:+.1f}% vs start)")
        self.bets_display.setText(f"{self.bets:,}")
        wl = f"{self.wins}-{self.losses}"
        if self.bets:
            wl += f"  ({self.wins/self.bets*100:.1f}%)"
        self.record_display.setText(wl)
        self.peak_display.setText(money(self.peak))
        self.peak_display.setToolTip(f"${self.peak:,.2f}")
        self.dd_display.setText(f"{self.max_drawdown*100:.1f}%")
        if self.bankroll > 0:
            stake = self._stake_for(self.bankroll)
            self.stake_display.setText(
                f"{money(stake)} ({stake / self.bankroll * 100:.2f}%)"
            )
            self.stake_display.setToolTip(
                f"${stake:,.2f} of a ${self.bankroll:,.2f} bankroll"
            )
        else:
            self.stake_display.setText("-")

    # ----------------------------------------------------------------------
    # UI wiring
    # ----------------------------------------------------------------------
    def _on_sizing_changed(self, idx):
        """Re-point the single sizing spin at the selected rule."""
        label, prefix, suffix, lo, hi, step, dec, _default, tip = self.SIZING_MODES[idx]
        self._sizing_mode = idx
        self.size_spin.blockSignals(True)
        self.size_spin.setPrefix(prefix)
        self.size_spin.setSuffix(suffix)
        self.size_spin.setDecimals(dec)
        self.size_spin.setRange(lo, hi)
        self.size_spin.setSingleStep(step)
        self.size_spin.setValue(self._size_values[idx])
        self.size_spin.blockSignals(False)
        self.size_spin.setToolTip(tip)
        self.recalculate()
        self.redraw()

    def _on_size_value_changed(self, value):
        self._size_values[self._sizing_mode] = value
        self.recalculate()

    def _on_speed_changed(self, value):
        """Slider is bets/sec; past ~60/s we batch bets into one 16ms frame."""
        bets_per_sec = int(round(1.08 ** value))  # 1/s .. ~2.2k/s
        if bets_per_sec <= 60:
            self._bets_per_tick = 1
            self._interval_ms = max(8, int(1000 / bets_per_sec))
        else:
            self._interval_ms = 16
            self._bets_per_tick = max(1, int(bets_per_sec / 60))
        self.speed_label.setText(f"{bets_per_sec:,}/s")
        if self.running:
            self.timer.start(self._interval_ms)

    # ----------------------------------------------------------------------
    # viewport
    # ----------------------------------------------------------------------
    def _follow_viewport(self, force=False):
        """Scroll/scale the view so the live end of the path stays framed.

        pyqtgraph's own auto-range is switched off while following: it fits the
        *whole* series (including the ghost curves of past runs), which is not
        what a moving window wants. X is a hard trailing window; Y is refit
        only when the visible slice escapes a comfort band inside the current
        range, otherwise a 33fps redraw makes the axis crawl every frame.
        """
        if not self.follow_check.isChecked() or not self.ys:
            return
        vb = self.plot.getPlotItem().vb

        # --- X: trailing window of `window_spin` bets -----------------------
        win = self.window_spin.value()
        n = self.bets
        if win <= 0:
            x0, x1 = 0.0, float(max(n, 10))
        else:
            x1 = float(max(n, win))
            x0 = x1 - win

        # --- Y: range of the slice actually on screen -----------------------
        i0 = max(0, int(x0))
        i1 = min(len(self.ys), int(x1) + 1)
        window_ys = self.ys[i0:i1] or self.ys[-1:]
        # a busted bankroll can be 0, whose log is -inf; floor it just under
        # the ruin line so the axis stays readable
        floor = self._ruin_level(self.start_bankroll) * 0.5
        dmin = max(min(window_ys), floor)
        dmax = max(max(window_ys), floor * 2)

        v0, v1 = self._y(dmin), self._y(dmax)

        # Let the expected-growth reference pull the frame, but by at most a
        # quarter of the data span each way — on a badly lagging path it
        # diverges without bound and would squash the bankroll into a flat
        # line. Past that the reference clips; the realised path is the
        # subject. Clamp against the data span itself, not a running value,
        # or each edge widens what the next edge is measured against.
        refs = [self._expected_at(ex) for ex in (max(x0, 0.0), min(x1, float(n)))]
        if all(r is not None for r in refs):
            allow = (v1 - v0) * 0.25
            rlo = self._y(max(min(refs), floor))
            rhi = self._y(max(max(refs), floor))
            v0 = min(v0, max(rlo, v0 - allow))
            v1 = max(v1, min(rhi, v1 + allow))

        span = v1 - v0
        if span <= 0:
            span = abs(v1) * 0.1 or (0.05 if self.log_check.isChecked() else 1.0)
        pad = span * 0.08
        target = (v0 - pad, v1 + pad)

        if not force and self._y_view is not None:
            cur0, cur1 = self._y_view
            cur_span = cur1 - cur0
            if cur_span > 0:
                margin = cur_span * 0.04
                framed = (cur0 + margin) <= v0 and v1 <= (cur1 - margin)
                # refit if the data escaped, or if it has shrunk into a
                # corner of a stale (over-wide) range
                if framed and span > cur_span * 0.7:
                    target = self._y_view

        vb.disableAutoRange()
        vb.setXRange(x0, x1, padding=0)
        vb.setYRange(*target, padding=0)
        self._y_view = target

    def _on_follow_toggled(self, on):
        if on:
            self._y_view = None
            self._follow_viewport(force=True)
        # turning it off leaves the current view alone — the user is driving

    def _on_range_changed_manually(self, _mask):
        """Hand pan/zoom wins: stop following rather than fight the mouse."""
        if self.follow_check.isChecked():
            self.follow_check.setChecked(False)

    def _on_log_toggled(self, on):
        vb = self.plot.getPlotItem().vb
        # carry the visible dollar range across the switch. Without this the
        # view keeps its old numbers in the new space — linear dollars read as
        # exponents overflow 10**x and the axis throws before we can refit.
        lo, hi = vb.viewRange()[1]
        if not on:  # leaving log space: exponents -> dollars
            lo, hi = 10 ** min(lo, 300.0), 10 ** min(hi, 300.0)
        else:       # entering log space: dollars -> exponents
            lo = math.log10(max(lo, 1e-9))
            hi = math.log10(max(hi, 1e-8))

        self.plot.setLogMode(False, on)
        vb.setYRange(lo, hi, padding=0)
        self.plot.setLabel("left", "Bankroll (log)" if on else "Bankroll", color="#aaa")
        self.start_line.setValue(self._y(self.start_bankroll))
        self.ruin_line.setValue(self._y(self._ruin_level(self.start_bankroll)))
        self._y_view = None
        self.redraw()
        if self.follow_check.isChecked():
            self._follow_viewport(force=True)

    def _on_mouse_moved(self, pos):
        vb = self.plot.getPlotItem().vb
        if not self.plot.sceneBoundingRect().contains(pos):
            self.cursor_text.setText("")
            return
        pt = vb.mapSceneToView(pos)
        n = int(round(pt.x()))
        if not self.xs or n < 0 or n > self.xs[-1]:
            self.cursor_text.setText("")
            return
        # xs is 0..bets contiguous, so index == bet number
        value = self.ys[min(n, len(self.ys) - 1)]
        self.vline.setPos(n)
        self.hline.setPos(self._y(value))
        pnl = value - self.start_bankroll
        self.cursor_text.setText(f"bet {n:,}   ${value:,.2f}   ({pnl:+,.2f})")
        self.cursor_text.setPos(n, self._y(value))


class CalculatorApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Odds Calculator")

        self.icon_frame = 0
        self.icon_timer = QTimer(self)
        self.icon_timer.setSingleShot(False)
        self.icon_timer.timeout.connect(self.UpdateIcon)
        self.icon_timer.start(16)

        self.resize(880, 720)

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

        # Add all four calculators
        self.odds_calculator = OddsConverterWidget()
        self.parlay_calculator = ParlayCalculator()
        self.multiway_calculator = MultiwayDevigger()
        self.kelly_calculator = KellyCalculator()

        self.stacked_widget.addWidget(self.odds_calculator)      # index 0
        self.stacked_widget.addWidget(self.parlay_calculator)     # index 1
        self.stacked_widget.addWidget(self.multiway_calculator)   # index 2
        self.stacked_widget.addWidget(self.kelly_calculator)      # index 3

        main_layout.addWidget(self.stacked_widget)

        # Mode definitions: (index, button_text_for_next, button_color, hover_color)
        self.modes = [
            (0, "Switch to Parlay Calculator", "#2196F3", "#1976D2"),
            (1, "Switch to Multiway Devigger", "#4CAF50", "#45a049"),
            (2, "Switch to Kelly Simulator", "#9C27B0", "#7B1FA2"),
            (3, "Switch to Odds Calculator", "#FF9800", "#F57C00"),
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
