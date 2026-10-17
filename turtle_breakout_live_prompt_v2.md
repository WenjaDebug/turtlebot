# Update: Turtle-Breakout-Strategie — Live-Test auf IBKR Paper-Konto

## Kontext
Live-/Paper-Test der Turtle-Breakout-Strategie (bisher "Hälfte 2" des Backtests) auf dem IBKR-Paper-Trading-Konto, Start 05.10.2026, Dauer 1 Woche. Ziel dieser Woche: prüfen, ob Signal-Erkennung, Positionsgrösse und Order-Ausführung über TWS/ib_async sauber funktionieren (Plumbing-Test) — NICHT, die Performance der Strategie zu bewerten. Eine Woche mit wenigen Trades sagt statistisch praktisch nichts über die Qualität der Strategie aus.

Diese Version ersetzt die Parameter aus dem ursprünglichen Backtest-Prompt für die Breakout-Hälfte. Alle Änderungen unten sind entweder durch die letzte Analyse (analyze_turtle.py, 1'047 simulierte Signale über 20 Start-Tage) begründet oder explizit als reine Testwochen-Anpassung markiert.

## 1. Aktien-Universe (geändert)
Statt nur S&P500 + Nasdaq-100: kombiniertes Universe aus
- S&P 500
- S&P 400 (MidCap)
- S&P 600 (SmallCap)
- Nasdaq Composite

Filter (beide müssen erfüllt sein):
- Marktkapitalisierung > 500 Mio. USD
- Durchschnittliches Tagesvolumen (20-Tage-Ø) > 5 Mio. USD, damit nur Titel mit ausreichender Liquidität für eine saubere Order-Ausführung in Frage kommen.

Universe-Liste 1x täglich vor Marktöffnung neu aufbauen (Konstituenten + Marktkap + Volumen aktualisieren), nicht fix cachen wie im Backtest — Survivorship-Bias ist hier kein Thema, weil live immer die aktuell gültige Liste verwendet wird.

## 2. Entry-Signal (Produktion, validiert)
- Breakout: Schlusskurs bricht über das höchste Hoch der letzten 20 Handelstage (Donchian-20).
- Volumenbestätigung (Filter, nicht Ranking-Kriterium): Tagesvolumen am Breakout-Tag > 1.5x des 20-Tage-Durchschnittsvolumens. Dieser Filter bleibt als Ja/Nein-Bedingung bestehen — die Analyse hat nur gezeigt, dass die Höhe des Volumen-Ratios (1.5x vs. 3x) keinen Unterschied für die Auswahl macht, nicht dass der Filter selbst nutzlos ist.

## 3. Ranking bei mehreren gleichzeitigen Signalen (geändert)
Bisher: Auswahl nach höchstem Volumen-Ratio. Das zeigte sich über 20 Start-Tage getestet als nicht robust (Median-Rendite ungefähr gleich wie simples 2x-gehebeltes SPY-Halten).

Neu: Ranking nach Breakout-Strength = (Schlusskurs − 20-Tage-Hoch-vor-Signal) / 20-Tage-Hoch-vor-Signal, höchster Wert zuerst. Das war über alle 20 getesteten Start-Tage klar überlegen und deutlich weniger pfadabhängig (Median +94% vs. +33%, Spannweite deutlich enger).

## 4. Positionsgrösse & Slots (geändert)
Bisher: fix $2'000/Position, max. 5 gleichzeitige Positionen — beides an das ursprüngliche $10'000-Konzept gebunden und zu eng für das Paper-Konto mit $1'000'000.

Neu, prozentual (damit die Regel unabhängig vom Kontostand gilt):
- Kapitalrahmen dieser Strategie: das volle Paper-Konto ($1'000'000). Falls du das anders willst (z.B. nur einen Teil reservieren), sag Bescheid — ich bin hier von "volles Konto" ausgegangen, weil du den Fixbetrag explizit als zu klein dafür bezeichnet hast.
- Positionsgrösse: 1.5% des Kapitalrahmens pro Position (aktuell $15'000/Position).
- Max. gleichzeitige Positionen (Slots): 20 (statt 5) — das erhöht nicht nur die Trade-Anzahl diese Woche, sondern reduziert auch die Pfadabhängigkeit, die im Backtest das Hauptproblem war (welche Signale zufällig zuerst die wenigen Slots belegen).
- Maximal gleichzeitig committed: 20 × 1.5% = 30% des Kapitalrahmens; die restlichen 70% bleiben als Cash-/Margin-Reserve für neue Signale.
- Cap gilt nur beim Entry einer neuen Position. Eine bereits offene Position wird NICHT zurückgestutzt, nur weil ihr Marktwert durch Kursgewinn über die ursprüngliche Positionsgrösse hinauswächst (siehe Diskussion zum $2'000-Cap-Bug im Backtest — gleiches Prinzip, jetzt korrekt von Anfang an umgesetzt).

## 5. Exit-Regel (unverändert, Produktion)
Exit bei Unterschreiten des 10-Tage-Tiefs (Donchian-Low-10). Die Analyse zeigte zwar einen leichten Vorteil für ein 20-Tage-Exit, aber mit grossem Vorbehalt (73 von getesteten Trades waren am Messzeitpunkt noch offen, starkes Trendjahr) — deshalb vorerst nicht übernommen.

## 6. Leverage / Margin (geändert — wichtig)
Im Backtest wurde 2x Leverage mit einer synthetischen Zinsannahme (5%/Jahr) simuliert, nur für die Chart-Darstellung. Live/Paper gilt das nicht mehr: IBKR bestimmt die tatsächlich verfügbare Kaufkraft über Reg-T-Margin selbst, abhängig vom Kontotyp. Keine fixe Hebel-Zahl im Code hart codieren — stattdessen über TWS/ib_async den Account Summary (verfügbare Margin, Buying Power) laufend abfragen und sicherstellen, dass keine Margin-Call-Schwelle erreicht wird. Falls dein Paper-Konto als Cash-Konto statt Margin-Konto geführt wird, ist ohnehin kein Hebel verfügbar (siehe unsere frühere Abklärung dazu).

## 7. Testmodus-Parameter — NUR für diese Live-Woche (05.–11.10.2026)
Diese Einstellungen dienen ausschliesslich dazu, diese Woche mehr Signale/Trades zur Kontrolle der technischen Pipeline zu erzeugen. Klar getrennt vom Produktions-Code halten (z.B. über ein `TEST_MODE`-Flag), nicht einfach die Produktionswerte überschreiben:
- Breakout-Lookback: 10 Tage statt 20 (mehr, aber schwächere Signale).
- Volumenfilter: > 1.3x statt > 1.5x des 20-Tage-Ø-Volumens.
- Scan-Frequenz: stündlich während der Handelszeiten (statt 1x täglich nach Börsenschluss), damit Breakouts nicht erst am nächsten Tag erfasst werden.

Nach Ende der Testwoche: TEST_MODE wieder deaktivieren und zu den Produktionswerten (Abschnitt 2) zurückkehren, bevor weitere Rückschlüsse auf die Strategie-Qualität gezogen werden.

## 8. Technische Hinweise
- Datenquelle für den täglichen/stündlichen Screen über das gesamte Universe (mehrere Tausend Ticker): weiterhin yfinance, nicht ib_async — IBKR-Historical-Data-Requests haben Pacing-Limits, die bei so vielen Tickern schnell greifen.
- Für die tatsächliche Order-Ausführung (sobald ein Kandidat ausgewählt ist): wie bisher TWS/ib_async, inkl. Bracket-Orders für den Exit.
- Universe-/Marktkap-/Volumen-Daten 1x täglich vor Marktöffnung neu laden, nicht über die ganze Woche cachen.

## 9. Einordnung
Diese Woche testet die Mechanik, nicht die Strategie. Bei vermutlich 0–5 Trades (je nach Lockerung in Abschnitt 7) lässt sich daraus keine verlässliche Aussage über Gewinnerwartung, Trefferquote oder Drawdown-Verhalten ableiten. Die eigentliche Validierung der Breakout-Strength-Ranking-Logik braucht einen längeren Backtest-Zeitraum inkl. einer schwächeren Marktphase (z.B. 2018–2026 mit zeitlich korrekten Index-Konstituenten), nicht eine einzelne Live-Woche in einem Bullenmarkt.
