//+------------------------------------------------------------------+
//|                                          XAU_EMA_RSI_Signal.mq5  |
//|  EMA cross + RSI + ATR filter signals for XAUUSD (M5 / H1)        |
//|  v1.1                                                             |
//|  - Confirmed Buy/Sell arrows on closed bars (no repaint)          |
//|  - Early-warning arrows on the live candle (can disappear)        |
//|  - Live BUY / SELL / WAIT bias label with reason                  |
//|  - Popup / push / sound alerts                                    |
//|  - ATR-based SL/TP levels for the latest confirmed signal         |
//|  - Lot size for % risk (works on USD and Cent accounts)           |
//+------------------------------------------------------------------+
#property copyright   "Josh"
#property version     "1.10"
#property description "EMA cross + RSI + ATR filter with live bias, early warnings and risk-based lots"
#property indicator_chart_window
#property indicator_buffers 9
#property indicator_plots   7

#property indicator_label1  "Buy"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrLime
#property indicator_width1  3

#property indicator_label2  "Sell"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrRed
#property indicator_width2  3

#property indicator_label3  "Possible Buy (live)"
#property indicator_type3   DRAW_ARROW
#property indicator_color3  clrPaleGreen
#property indicator_width3  1

#property indicator_label4  "Possible Sell (live)"
#property indicator_type4   DRAW_ARROW
#property indicator_color4  clrLightPink
#property indicator_width4  1

#property indicator_label5  "Fast EMA"
#property indicator_type5   DRAW_LINE
#property indicator_color5  clrDodgerBlue
#property indicator_width5  1

#property indicator_label6  "Slow EMA"
#property indicator_type6   DRAW_LINE
#property indicator_color6  clrOrange
#property indicator_width6  1

#property indicator_label7  "Trend EMA"
#property indicator_type7   DRAW_LINE
#property indicator_color7  clrSilver
#property indicator_style7  STYLE_DOT
#property indicator_width7  1

//--- enums
enum ENUM_PRESET
  {
   PRESET_AUTO   = 0, // Auto (tuned for M5 / H1)
   PRESET_CUSTOM = 1  // Custom (use inputs below)
  };

enum ENUM_ACCT
  {
   ACCT_AUTO     = 0, // Auto-detect from account currency
   ACCT_STANDARD = 1, // Standard (USD)
   ACCT_CENT     = 2  // Cent (USC)
  };

//--- inputs
input group "Preset"
input ENUM_PRESET InpPreset        = PRESET_AUTO; // Preset
input ENUM_ACCT   InpAccountType   = ACCT_AUTO;   // Account type

input group "Signal"
input int    InpFastEMA        = 9;     // Fast EMA
input int    InpSlowEMA        = 21;    // Slow EMA
input int    InpTrendEMA       = 200;   // Trend EMA
input bool   InpUseTrendFilter = true;  // Only trade with Trend EMA
input int    InpRSIPeriod      = 14;    // RSI period
input double InpRSIMid         = 50.0;  // RSI midline
input double InpRSIUpper       = 70.0;  // No buys above (overbought)
input double InpRSILower       = 30.0;  // No sells below (oversold)

input group "Volatility filter (Custom preset)"
input int    InpATRPeriod      = 14;    // ATR period
input int    InpATRAvgPeriod   = 50;    // ATR average lookback
input double InpATRFloor       = 0.8;   // Min ATR as fraction of its average

input group "Risk (Custom preset for SL multiplier)"
input double InpSL_ATR         = 1.5;   // SL = ATR x this
input double InpRR             = 2.0;   // TP = SL distance x this
input double InpRiskPct        = 1.0;   // Risk % of balance per trade

input group "Session filter, server time (Custom preset)"
input bool   InpUseSession     = false; // Use session filter
input int    InpSessionStart   = 9;     // Session start hour
input int    InpSessionEnd     = 22;    // Session end hour

input group "Alerts & display"
input bool   InpPopup          = true;  // Popup alert (confirmed)
input bool   InpPush           = true;  // Push to phone (confirmed)
input bool   InpSound          = false; // Sound (confirmed)
input bool   InpEarlyArrows    = true;  // Show live-candle early warnings
input bool   InpEarlyAlert     = true;  // Popup alert on early warning (once per bar)
input bool   InpEarlyPush      = false; // Push on early warning
input bool   InpShowBias       = true;  // Show BUY/SELL/WAIT bias label
input bool   InpShowLevels     = true;  // Draw Entry/SL/TP
input bool   InpShowPanel      = true;  // Show info panel
input bool   InpShowEMAs       = true;  // Show EMA lines

//--- buffers
double BuyBuf[], SellBuf[], EarlyBuyBuf[], EarlySellBuf[];
double FastBuf[], SlowBuf[], TrendBuf[], RSIBuf[], ATRBuf[];

//--- handles
int hFast = INVALID_HANDLE, hSlow = INVALID_HANDLE, hTrend = INVALID_HANDLE;
int hRSI  = INVALID_HANDLE, hATR  = INVALID_HANDLE;

//--- working settings
double g_slATR;
double g_atrFloor;
bool   g_useSession;
int    g_sessStart, g_sessEnd;
int    g_minBars;
string g_prefix   = "XERS_";
string g_tf;
string g_presetName;

//--- state: last confirmed signal
datetime g_lastAlert = 0;
int      g_lastDir   = 0;
datetime g_lastTime  = 0;
double   g_lastEntry = 0.0;
double   g_lastATR   = 0.0;

//--- state: live
int      g_bias        = 0;
string   g_biasReason  = "";
int      g_earlyDir    = 0;
datetime g_lastEarlyAlert = 0;
bool     g_sessionOpen = true;
bool     g_volOK       = true;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpFastEMA >= InpSlowEMA)
     {
      Print("Fast EMA must be smaller than Slow EMA");
      return(INIT_PARAMETERS_INCORRECT);
     }

//--- presets
   bool scalpTF = (PeriodSeconds() <= 900); // M15 and below
   if(InpPreset == PRESET_AUTO)
     {
      if(scalpTF)
        {
         g_slATR      = 2.0;   // wider stop: M5 gold noise
         g_atrFloor   = 1.0;   // only trade when volatility is at/above average
         g_useSession = true;  // London + NY only
         g_sessStart  = 9;
         g_sessEnd    = 22;
         g_presetName = "Auto M5";
        }
      else
        {
         g_slATR      = 1.5;
         g_atrFloor   = 0.8;
         g_useSession = false;
         g_sessStart  = InpSessionStart;
         g_sessEnd    = InpSessionEnd;
         g_presetName = "Auto H1";
        }
     }
   else
     {
      g_slATR      = InpSL_ATR;
      g_atrFloor   = InpATRFloor;
      g_useSession = InpUseSession;
      g_sessStart  = InpSessionStart;
      g_sessEnd    = InpSessionEnd;
      g_presetName = "Custom";
     }

   g_minBars = MathMax(InpTrendEMA, InpATRAvgPeriod + InpATRPeriod) + 5;

   g_tf = EnumToString((ENUM_TIMEFRAMES)_Period);
   StringReplace(g_tf, "PERIOD_", "");

//--- buffers
   SetIndexBuffer(0, BuyBuf,       INDICATOR_DATA);
   SetIndexBuffer(1, SellBuf,      INDICATOR_DATA);
   SetIndexBuffer(2, EarlyBuyBuf,  INDICATOR_DATA);
   SetIndexBuffer(3, EarlySellBuf, INDICATOR_DATA);
   SetIndexBuffer(4, FastBuf,      INDICATOR_DATA);
   SetIndexBuffer(5, SlowBuf,      INDICATOR_DATA);
   SetIndexBuffer(6, TrendBuf,     INDICATOR_DATA);
   SetIndexBuffer(7, RSIBuf,       INDICATOR_CALCULATIONS);
   SetIndexBuffer(8, ATRBuf,       INDICATOR_CALCULATIONS);

   PlotIndexSetInteger(0, PLOT_ARROW, 233); // filled up arrow
   PlotIndexSetInteger(1, PLOT_ARROW, 234); // filled down arrow
   PlotIndexSetInteger(2, PLOT_ARROW, 225); // thin up arrow
   PlotIndexSetInteger(3, PLOT_ARROW, 226); // thin down arrow
   for(int p = 0; p < 4; p++)
      PlotIndexSetDouble(p, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   PlotIndexSetInteger(4, PLOT_DRAW_BEGIN, InpSlowEMA);
   PlotIndexSetInteger(5, PLOT_DRAW_BEGIN, InpSlowEMA);
   PlotIndexSetInteger(6, PLOT_DRAW_BEGIN, InpTrendEMA);

   if(!InpEarlyArrows)
     {
      PlotIndexSetInteger(2, PLOT_DRAW_TYPE, DRAW_NONE);
      PlotIndexSetInteger(3, PLOT_DRAW_TYPE, DRAW_NONE);
     }
   if(!InpShowEMAs)
     {
      PlotIndexSetInteger(4, PLOT_DRAW_TYPE, DRAW_NONE);
      PlotIndexSetInteger(5, PLOT_DRAW_TYPE, DRAW_NONE);
      PlotIndexSetInteger(6, PLOT_DRAW_TYPE, DRAW_NONE);
     }
   if(!InpUseTrendFilter)
      PlotIndexSetInteger(6, PLOT_DRAW_TYPE, DRAW_NONE);

//--- handles
   hFast  = iMA(_Symbol, _Period, InpFastEMA,  0, MODE_EMA, PRICE_CLOSE);
   hSlow  = iMA(_Symbol, _Period, InpSlowEMA,  0, MODE_EMA, PRICE_CLOSE);
   hTrend = iMA(_Symbol, _Period, InpTrendEMA, 0, MODE_EMA, PRICE_CLOSE);
   hRSI   = iRSI(_Symbol, _Period, InpRSIPeriod, PRICE_CLOSE);
   hATR   = iATR(_Symbol, _Period, InpATRPeriod);

   if(hFast == INVALID_HANDLE || hSlow == INVALID_HANDLE || hTrend == INVALID_HANDLE ||
      hRSI == INVALID_HANDLE || hATR == INVALID_HANDLE)
     {
      Print("Failed to create indicator handles, error ", GetLastError());
      return(INIT_FAILED);
     }

   IndicatorSetString(INDICATOR_SHORTNAME,
                      StringFormat("XAU EMA/RSI (%d/%d/%d, %s)", InpFastEMA, InpSlowEMA, InpTrendEMA, g_presetName));
   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);

   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, g_prefix);
   Comment("");
   if(hFast  != INVALID_HANDLE) IndicatorRelease(hFast);
   if(hSlow  != INVALID_HANDLE) IndicatorRelease(hSlow);
   if(hTrend != INVALID_HANDLE) IndicatorRelease(hTrend);
   if(hRSI   != INVALID_HANDLE) IndicatorRelease(hRSI);
   if(hATR   != INVALID_HANDLE) IndicatorRelease(hATR);
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   if(rates_total < g_minBars + 2)
      return(0);

   if(BarsCalculated(hFast)  < rates_total || BarsCalculated(hSlow) < rates_total ||
      BarsCalculated(hTrend) < rates_total || BarsCalculated(hRSI)  < rates_total ||
      BarsCalculated(hATR)   < rates_total)
      return(0);

   int to_copy;
   if(prev_calculated <= 0 || prev_calculated > rates_total)
      to_copy = rates_total;
   else
      to_copy = rates_total - prev_calculated + 1;

   if(CopyBuffer(hFast,  0, 0, to_copy, FastBuf)  <= 0) return(0);
   if(CopyBuffer(hSlow,  0, 0, to_copy, SlowBuf)  <= 0) return(0);
   if(CopyBuffer(hTrend, 0, 0, to_copy, TrendBuf) <= 0) return(0);
   if(CopyBuffer(hRSI,   0, 0, to_copy, RSIBuf)   <= 0) return(0);
   if(CopyBuffer(hATR,   0, 0, to_copy, ATRBuf)   <= 0) return(0);

   int start;
   if(prev_calculated <= 0 || prev_calculated > rates_total)
     {
      ArrayInitialize(BuyBuf,       EMPTY_VALUE);
      ArrayInitialize(SellBuf,      EMPTY_VALUE);
      ArrayInitialize(EarlyBuyBuf,  EMPTY_VALUE);
      ArrayInitialize(EarlySellBuf, EMPTY_VALUE);
      start = g_minBars;
     }
   else
      start = prev_calculated - 1;

   int live = rates_total - 1;

   for(int i = start; i < rates_total; i++)
     {
      BuyBuf[i]       = EMPTY_VALUE;
      SellBuf[i]      = EMPTY_VALUE;
      EarlyBuyBuf[i]  = EMPTY_VALUE;
      EarlySellBuf[i] = EMPTY_VALUE;

      int sig = SignalAt(i, time, close);

      if(i < live)
        {
         //--- confirmed (closed bar)
         if(sig == 1)
            BuyBuf[i]  = low[i]  - ATRBuf[i] * 0.3;
         else if(sig == -1)
            SellBuf[i] = high[i] + ATRBuf[i] * 0.3;
        }
      else if(InpEarlyArrows)
        {
         //--- live candle: early warning, recalculated every tick
         if(sig == 1)
            EarlyBuyBuf[i]  = low[i]  - ATRBuf[i] * 0.6;
         else if(sig == -1)
            EarlySellBuf[i] = high[i] + ATRBuf[i] * 0.6;
        }
     }

//--- live state
   g_earlyDir    = (EarlyBuyBuf[live] != EMPTY_VALUE) ? 1 : (EarlySellBuf[live] != EMPTY_VALUE ? -1 : 0);
   g_sessionOpen = SessionOK(time[live]);
   g_volOK       = VolOK(live);
   g_bias        = BiasAt(live, close, g_biasReason);

   bool newClosedBar = (start <= rates_total - 2);

   if(newClosedBar || prev_calculated <= 0)
     {
      FindLastSignal(rates_total, time, close);
      if(InpShowLevels)
         DrawLevels();
     }

//--- confirmed alert once per closed signal bar (skip on first load)
   if(prev_calculated > 0 && newClosedBar)
     {
      int b = rates_total - 2;
      if((BuyBuf[b] != EMPTY_VALUE || SellBuf[b] != EMPTY_VALUE) && time[b] != g_lastAlert)
        {
         g_lastAlert = time[b];
         FireAlert();
        }
     }

//--- early-warning alert once per live bar
   if(prev_calculated > 0 && g_earlyDir != 0 && time[live] != g_lastEarlyAlert)
     {
      g_lastEarlyAlert = time[live];
      string msg = StringFormat("%s %s POSSIBLE %s forming @ %s (not confirmed until candle closes)",
                                _Symbol, g_tf, (g_earlyDir == 1 ? "BUY" : "SELL"),
                                DoubleToString(close[live], _Digits));
      if(InpEarlyAlert) Alert(msg);
      if(InpEarlyPush)  SendNotification(msg);
     }

   if(InpShowBias)
      DrawBias();
   if(InpShowPanel)
      UpdatePanel();

   return(rates_total);
  }

//+------------------------------------------------------------------+
//| Filters                                                          |
//+------------------------------------------------------------------+
bool InSession(const int hour)
  {
   if(g_sessStart <= g_sessEnd)
      return(hour >= g_sessStart && hour < g_sessEnd);
   return(hour >= g_sessStart || hour < g_sessEnd); // wraps midnight
  }

bool SessionOK(const datetime t)
  {
   if(!g_useSession)
      return(true);
   MqlDateTime dt;
   TimeToStruct(t, dt);
   return(InSession(dt.hour));
  }

bool VolOK(const int i)
  {
   if(i < InpATRAvgPeriod)
      return(false);
   double sum = 0.0;
   for(int k = 0; k < InpATRAvgPeriod; k++)
      sum += ATRBuf[i - k];
   double atrAvg = sum / InpATRAvgPeriod;
   return(ATRBuf[i] >= atrAvg * g_atrFloor);
  }

//+------------------------------------------------------------------+
//| Crossover signal (closed or live bar)                            |
//+------------------------------------------------------------------+
int SignalAt(const int i, const datetime &time[], const double &close[])
  {
   if(i < 1)
      return(0);

   bool crossUp = (FastBuf[i] > SlowBuf[i] && FastBuf[i - 1] <= SlowBuf[i - 1]);
   bool crossDn = (FastBuf[i] < SlowBuf[i] && FastBuf[i - 1] >= SlowBuf[i - 1]);
   if(!crossUp && !crossDn)
      return(0);

   if(!VolOK(i))
      return(0);
   if(!SessionOK(time[i]))
      return(0);

   double rsi = RSIBuf[i];

   if(crossUp && rsi > InpRSIMid && rsi < InpRSIUpper &&
      (!InpUseTrendFilter || close[i] > TrendBuf[i]))
      return(1);

   if(crossDn && rsi < InpRSIMid && rsi > InpRSILower &&
      (!InpUseTrendFilter || close[i] < TrendBuf[i]))
      return(-1);

   return(0);
  }

//+------------------------------------------------------------------+
//| Live bias: direction the setup currently favours                  |
//+------------------------------------------------------------------+
int BiasAt(const int i, const double &close[], string &reason)
  {
   bool   emaUp   = FastBuf[i] > SlowBuf[i];
   bool   trendUp = close[i] > TrendBuf[i];
   double rsi     = RSIBuf[i];

   bool bullStack = emaUp  && (!InpUseTrendFilter || trendUp);
   bool bearStack = !emaUp && (!InpUseTrendFilter || !trendUp);

   int dir = 0;

   if(bullStack && rsi > InpRSIMid)
     {
      dir = 1;
      reason = (rsi >= InpRSIUpper) ? "Uptrend, but RSI overbought: wait for pullback"
                                    : "EMA 9 > 21, price > EMA 200, RSI > 50";
     }
   else if(bearStack && rsi < InpRSIMid)
     {
      dir = -1;
      reason = (rsi <= InpRSILower) ? "Downtrend, but RSI oversold: wait for pullback"
                                    : "EMA 9 < 21, price < EMA 200, RSI < 50";
     }
   else
     {
      if(emaUp && InpUseTrendFilter && !trendUp)
         reason = "EMAs bullish but price below EMA 200 (conflict)";
      else if(!emaUp && InpUseTrendFilter && trendUp)
         reason = "EMAs bearish but price above EMA 200 (conflict)";
      else
         reason = "RSI disagrees with EMAs (momentum fading)";
     }

   if(!g_sessionOpen)
      reason += " | Session closed";
   else if(!g_volOK)
      reason += " | Low volatility";

   return(dir);
  }

void DrawBias()
  {
   string n1 = g_prefix + "bias";
   string n2 = g_prefix + "bias_r";

   string txt;
   color  clr;
   if(g_bias == 1)       { txt = "BUY BIAS";  clr = clrLime; }
   else if(g_bias == -1) { txt = "SELL BIAS"; clr = clrRed;  }
   else                  { txt = "WAIT";      clr = clrGold; }

   if(g_earlyDir == 1)       txt += "  |  possible BUY forming";
   else if(g_earlyDir == -1) txt += "  |  possible SELL forming";

   if(ObjectFind(0, n1) < 0)
      ObjectCreate(0, n1, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, n1, OBJPROP_CORNER,     CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, n1, OBJPROP_ANCHOR,     ANCHOR_RIGHT_UPPER);
   ObjectSetInteger(0, n1, OBJPROP_XDISTANCE,  12);
   ObjectSetInteger(0, n1, OBJPROP_YDISTANCE,  20);
   ObjectSetInteger(0, n1, OBJPROP_FONTSIZE,   16);
   ObjectSetString(0,  n1, OBJPROP_FONT,       "Arial Bold");
   ObjectSetInteger(0, n1, OBJPROP_COLOR,      clr);
   ObjectSetInteger(0, n1, OBJPROP_SELECTABLE, false);
   ObjectSetString(0,  n1, OBJPROP_TEXT,       txt);

   if(ObjectFind(0, n2) < 0)
      ObjectCreate(0, n2, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, n2, OBJPROP_CORNER,     CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, n2, OBJPROP_ANCHOR,     ANCHOR_RIGHT_UPPER);
   ObjectSetInteger(0, n2, OBJPROP_XDISTANCE,  12);
   ObjectSetInteger(0, n2, OBJPROP_YDISTANCE,  48);
   ObjectSetInteger(0, n2, OBJPROP_FONTSIZE,   9);
   ObjectSetString(0,  n2, OBJPROP_FONT,       "Arial");
   ObjectSetInteger(0, n2, OBJPROP_COLOR,      clrWhiteSmoke);
   ObjectSetInteger(0, n2, OBJPROP_SELECTABLE, false);
   ObjectSetString(0,  n2, OBJPROP_TEXT,       g_biasReason);
  }

//+------------------------------------------------------------------+
void FindLastSignal(const int rates_total, const datetime &time[], const double &close[])
  {
   g_lastDir = 0;
   int stop = MathMax(g_minBars, rates_total - 2000);
   for(int i = rates_total - 2; i >= stop; i--)
     {
      if(BuyBuf[i] != EMPTY_VALUE || SellBuf[i] != EMPTY_VALUE)
        {
         g_lastDir   = (BuyBuf[i] != EMPTY_VALUE) ? 1 : -1;
         g_lastTime  = time[i];
         g_lastEntry = close[i];
         g_lastATR   = ATRBuf[i];
         return;
        }
     }
  }

//+------------------------------------------------------------------+
//| Account helpers                                                  |
//+------------------------------------------------------------------+
bool IsCent()
  {
   if(InpAccountType == ACCT_CENT)     return(true);
   if(InpAccountType == ACCT_STANDARD) return(false);
   string cu = AccountInfoString(ACCOUNT_CURRENCY);
   StringToUpper(cu);
   return(cu == "USC" || StringFind(cu, "CENT") >= 0);
  }

string Money(const double v)
  {
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   string s = DoubleToString(v, 2) + " " + cur;
   if(IsCent())
      s += " (~$" + DoubleToString(v / 100.0, 2) + ")";
   return(s);
  }

int VolDigits(const double step)
  {
   int d = 0;
   double s = step;
   while(s < 1.0 - 1e-9 && d < 8)
     {
      s *= 10.0;
      d++;
     }
   return(d);
  }

//+------------------------------------------------------------------+
//| Lot size for % risk. Tick value is reported in account currency,  |
//| so USD and USC accounts both work.                                |
//+------------------------------------------------------------------+
double CalcLots(const double slDist, double &riskMoney, double &actualRisk, bool &belowMin)
  {
   belowMin   = false;
   actualRisk = 0.0;
   riskMoney  = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPct / 100.0;

   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tickVal <= 0.0)
      tickVal = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   if(tickSize <= 0.0 || tickVal <= 0.0 || slDist <= 0.0)
      return(0.0);

   double lossPerLot = (slDist / tickSize) * tickVal;
   double raw        = riskMoney / lossPerLot;

   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minL = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxL = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0.0)
      step = 0.01;

   double lots = MathFloor(raw / step + 1e-9) * step;
   if(lots < minL)
     {
      lots     = minL;
      belowMin = true;
     }
   if(lots > maxL)
      lots = maxL;

   lots       = NormalizeDouble(lots, VolDigits(step));
   actualRisk = lots * lossPerLot;
   return(lots);
  }

//+------------------------------------------------------------------+
//| Levels                                                           |
//+------------------------------------------------------------------+
void DrawLevel(const string tag, const datetime t, const double price,
               const color clr, const ENUM_LINE_STYLE st, const string label)
  {
   string   n  = g_prefix + "lvl_" + tag;
   datetime t2 = t + PeriodSeconds() * 25;

   if(ObjectFind(0, n) < 0)
      ObjectCreate(0, n, OBJ_TREND, 0, t, price, t2, price);
   else
     {
      ObjectMove(0, n, 0, t,  price);
      ObjectMove(0, n, 1, t2, price);
     }
   ObjectSetInteger(0, n, OBJPROP_COLOR,      clr);
   ObjectSetInteger(0, n, OBJPROP_STYLE,      st);
   ObjectSetInteger(0, n, OBJPROP_WIDTH,      1);
   ObjectSetInteger(0, n, OBJPROP_RAY_RIGHT,  false);
   ObjectSetInteger(0, n, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, n, OBJPROP_BACK,       true);

   string tn = n + "_t";
   if(ObjectFind(0, tn) < 0)
      ObjectCreate(0, tn, OBJ_TEXT, 0, t2, price);
   else
      ObjectMove(0, tn, 0, t2, price);
   ObjectSetString(0,  tn, OBJPROP_TEXT,       label);
   ObjectSetInteger(0, tn, OBJPROP_COLOR,      clr);
   ObjectSetInteger(0, tn, OBJPROP_FONTSIZE,   8);
   ObjectSetInteger(0, tn, OBJPROP_ANCHOR,     ANCHOR_LEFT);
   ObjectSetInteger(0, tn, OBJPROP_SELECTABLE, false);
  }

void DrawLevels()
  {
   if(g_lastDir == 0)
     {
      ObjectsDeleteAll(0, g_prefix + "lvl_");
      return;
     }
   double slDist = g_lastATR * g_slATR;
   double sl     = g_lastEntry - g_lastDir * slDist;
   double tp     = g_lastEntry + g_lastDir * slDist * InpRR;

   DrawLevel("entry", g_lastTime, g_lastEntry, clrWhite,  STYLE_SOLID,
             (g_lastDir == 1 ? "BUY " : "SELL ") + DoubleToString(g_lastEntry, _Digits));
   DrawLevel("sl",    g_lastTime, sl,          clrTomato, STYLE_DASH,
             "SL " + DoubleToString(sl, _Digits));
   DrawLevel("tp",    g_lastTime, tp,          clrLime,   STYLE_DASH,
             "TP " + DoubleToString(tp, _Digits));
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Alerts                                                           |
//+------------------------------------------------------------------+
void FireAlert()
  {
   if(g_lastDir == 0)
      return;
   double slDist = g_lastATR * g_slATR;
   double sl     = g_lastEntry - g_lastDir * slDist;
   double tp     = g_lastEntry + g_lastDir * slDist * InpRR;
   double riskMoney, actualRisk;
   bool   belowMin;
   double lots = CalcLots(slDist, riskMoney, actualRisk, belowMin);

   string msg = StringFormat("%s %s CONFIRMED %s @ %s | SL %s | TP %s | Lots %s%s",
                             _Symbol, g_tf, (g_lastDir == 1 ? "BUY" : "SELL"),
                             DoubleToString(g_lastEntry, _Digits),
                             DoubleToString(sl, _Digits),
                             DoubleToString(tp, _Digits),
                             DoubleToString(lots, VolDigits(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP))),
                             (belowMin ? " (min lot > risk!)" : ""));
   if(InpPopup) Alert(msg);
   if(InpPush)  SendNotification(msg);
   if(InpSound) PlaySound("alert.wav");
  }

//+------------------------------------------------------------------+
//| Info panel                                                       |
//+------------------------------------------------------------------+
void UpdatePanel()
  {
   string acct = IsCent() ? "Standard Cent" : "Standard USD";
   string txt  = StringFormat("XAU EMA/RSI Signal v1.1 | %s | %s\n", g_tf, g_presetName);
   txt += "Account: " + acct + " | Balance: " + Money(AccountInfoDouble(ACCOUNT_BALANCE)) + "\n";

   string sess = !g_useSession ? "off" :
                 (g_sessionOpen ? StringFormat("OPEN (%02d:00-%02d:00 server)", g_sessStart, g_sessEnd)
                                : StringFormat("CLOSED (opens %02d:00 server)", g_sessStart));
   txt += "Session filter: " + sess + " | Volatility: " + (g_volOK ? "OK" : "LOW") + "\n";

   if(g_lastDir == 0)
     {
      txt += "No confirmed signal in recent history.";
      Comment(txt);
      return;
     }

   double slDist = g_lastATR * g_slATR;
   double sl     = g_lastEntry - g_lastDir * slDist;
   double tp     = g_lastEntry + g_lastDir * slDist * InpRR;
   double riskMoney, actualRisk;
   bool   belowMin;
   double lots   = CalcLots(slDist, riskMoney, actualRisk, belowMin);
   double bal    = AccountInfoDouble(ACCOUNT_BALANCE);
   double actPct = (bal > 0.0) ? actualRisk / bal * 100.0 : 0.0;

   txt += StringFormat("Last confirmed: %s @ %s  (%s)\n",
                       (g_lastDir == 1 ? "BUY" : "SELL"),
                       DoubleToString(g_lastEntry, _Digits),
                       TimeToString(g_lastTime, TIME_DATE | TIME_MINUTES));
   txt += StringFormat("SL %s (dist %s)  TP %s  RR 1:%.1f\n",
                       DoubleToString(sl, _Digits), DoubleToString(slDist, _Digits),
                       DoubleToString(tp, _Digits), InpRR);
   txt += StringFormat("Target risk %.2f%% = %s\n", InpRiskPct, Money(riskMoney));
   txt += "Lots: " + DoubleToString(lots, VolDigits(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP))) +
          " -> actual risk " + Money(actualRisk) + StringFormat(" (%.2f%%)", actPct);
   if(belowMin)
      txt += "\n!! Minimum lot risks more than your target %";

   Comment(txt);
  }
//+------------------------------------------------------------------+
