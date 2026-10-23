//+------------------------------------------------------------------+
//|                                                  DumpSignals.mq5 |
//|  Script: dumps XAU_EMA_RSI_Signal confirmed signals plus the     |
//|  built-in iMA / iRSI / iATR values for every bar to a CSV in     |
//|  MQL5/Files, for the Python parity test (xauai.parity).          |
//|  Run on any chart; symbol and timeframe come from the inputs.    |
//+------------------------------------------------------------------+
#property copyright   "Josh"
#property version     "1.00"
#property description "Dump indicator signals + EMA/RSI/ATR to CSV for Python parity"
#property script_show_inputs

input string          InpSymbol    = "XAUUSD.sc";                  // Symbol
input ENUM_TIMEFRAMES InpTF        = PERIOD_H1;                    // Timeframe
input string          InpIndicator = "XAU_EMA_RSI_Signal";         // Indicator (under MQL5/Indicators)
input string          InpFile      = "xauai_dump_signals_h1.csv";  // Output file (MQL5/Files)
input int             InpFastEMA   = 9;                            // Fast EMA (must match indicator)
input int             InpSlowEMA   = 21;                           // Slow EMA
input int             InpTrendEMA  = 200;                          // Trend EMA
input int             InpRSIPeriod = 14;                           // RSI period
input int             InpATRPeriod = 14;                           // ATR period
input int             InpTimeoutMs = 120000;                       // Max wait for indicator calculation

//+------------------------------------------------------------------+
bool WaitCalculated(const int handle, const int bars)
  {
   uint t0 = GetTickCount();
   while(BarsCalculated(handle) < bars)
     {
      if(IsStopped() || (int)(GetTickCount() - t0) > InpTimeoutMs)
         return(false);
      Sleep(100);
     }
   return(true);
  }

//+------------------------------------------------------------------+
void OnStart()
  {
   if(!SymbolSelect(InpSymbol, true))
     {
      Print("SymbolSelect failed for ", InpSymbol);
      return;
     }

//--- default inputs => indicator's Auto preset for this timeframe
   int hSig   = iCustom(InpSymbol, InpTF, InpIndicator);
   int hFast  = iMA(InpSymbol, InpTF, InpFastEMA,  0, MODE_EMA, PRICE_CLOSE);
   int hSlow  = iMA(InpSymbol, InpTF, InpSlowEMA,  0, MODE_EMA, PRICE_CLOSE);
   int hTrend = iMA(InpSymbol, InpTF, InpTrendEMA, 0, MODE_EMA, PRICE_CLOSE);
   int hRSI   = iRSI(InpSymbol, InpTF, InpRSIPeriod, PRICE_CLOSE);
   int hATR   = iATR(InpSymbol, InpTF, InpATRPeriod);
   int handles[] = {hSig, hFast, hSlow, hTrend, hRSI, hATR};

   for(int k = 0; k < ArraySize(handles); k++)
      if(handles[k] == INVALID_HANDLE)
        {
         Print("Failed to create handle #", k, ", error ", GetLastError());
         ReleaseAll(handles);
         return;
        }

   int bars = Bars(InpSymbol, InpTF);
   for(int k = 0; k < ArraySize(handles); k++)
      if(!WaitCalculated(handles[k], bars))
        {
         Print("Timed out waiting for handle #", k, " (", BarsCalculated(handles[k]), "/", bars, " bars)");
         ReleaseAll(handles);
         return;
        }

   MqlRates rates[];
   double buy[], sell[], fast[], slow[], trend[], rsi[], atr[];
   int n = CopyRates(InpSymbol, InpTF, 0, bars, rates);
   bool ok = n > 0
             && CopyBuffer(hSig,   0, 0, n, buy)   == n
             && CopyBuffer(hSig,   1, 0, n, sell)  == n
             && CopyBuffer(hFast,  0, 0, n, fast)  == n
             && CopyBuffer(hSlow,  0, 0, n, slow)  == n
             && CopyBuffer(hTrend, 0, 0, n, trend) == n
             && CopyBuffer(hRSI,   0, 0, n, rsi)   == n
             && CopyBuffer(hATR,   0, 0, n, atr)   == n;
   if(!ok)
     {
      Print("Copy failed (rates ", n, "), error ", GetLastError());
      ReleaseAll(handles);
      return;
     }

   int f = FileOpen(InpFile, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(f == INVALID_HANDLE)
     {
      Print("FileOpen failed, error ", GetLastError());
      ReleaseAll(handles);
      return;
     }

//--- time = bar open, epoch seconds in server time. Last row is the forming bar.
   FileWrite(f, "time", "open", "high", "low", "close", "ema_fast", "ema_slow", "ema_trend",
             "rsi", "atr", "buy", "sell");
   int signals = 0;
   for(int i = 0; i < n; i++)
     {
      int b = (buy[i]  != EMPTY_VALUE) ? 1 : 0;
      int s = (sell[i] != EMPTY_VALUE) ? 1 : 0;
      signals += b + s;
      FileWrite(f, (long)rates[i].time,
                DoubleToString(rates[i].open, _Digits), DoubleToString(rates[i].high, _Digits),
                DoubleToString(rates[i].low, _Digits),  DoubleToString(rates[i].close, _Digits),
                DoubleToString(fast[i], 10), DoubleToString(slow[i], 10), DoubleToString(trend[i], 10),
                DoubleToString(rsi[i], 10),  DoubleToString(atr[i], 10), b, s);
     }
   FileClose(f);
   ReleaseAll(handles);

   PrintFormat("DumpSignals: %d bars, %d signals, %s .. %s -> MQL5/Files/%s",
               n, signals, TimeToString(rates[0].time), TimeToString(rates[n - 1].time), InpFile);
  }

//+------------------------------------------------------------------+
void ReleaseAll(int &handles[])
  {
   for(int k = 0; k < ArraySize(handles); k++)
      if(handles[k] != INVALID_HANDLE)
         IndicatorRelease(handles[k]);
  }
//+------------------------------------------------------------------+
