# RFD TradingView Logic Telegram Bot

هذا البوت يحول منطق مؤشر TradingView إلى تنبيهات تليجرام.

## المنصات
- OKX
- Gate.io
- Bybit
- CoinMarketCap كفلتر لقائمة العملات فقط

## الملفات
- `bot.py`
- `requirements.txt`
- `.env.example`
- `Procfile`

## طريقة التشغيل على Railway
1. ارفع الملفات على GitHub.
2. اربط GitHub مع Railway.
3. أضف المتغيرات من `.env.example` داخل Variables.
4. شغل المشروع.

## منطق التنبيه
### Long
- Daily close diff > THRESHOLD
- RSI > 30

### Short
- Daily close diff < -THRESHOLD
- RSI < 70

## ملاحظة
CoinMarketCap لا يستخدم كمصدر شموع، فقط لتحديد/فلترة العملات.
