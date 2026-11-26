# Troubleshooting

Short fixes for the most common problems. The full reference is in [README.md](../README.md). The Arabic version is in the second half of this file.

## The bot does not answer

1. Is the `start_assistant.bat` window still open? The bot runs only while that window is open. Its window shows the reason if the bot stopped.
2. Are you in the bot's private chat? Press **Start** there and send `/start`. Groups are ignored.
3. Is another copy running with the same token? Only one copy receives messages. Close the other copy and start again.
4. Is your Telegram user ID in `allowed_user_ids`? See [Wrong user ID](#wrong-user-id). The bot never replies to other accounts, so a stranger sees nothing.
5. Can this PC reach `api.telegram.org`? See [Telegram blocked or a proxy](#telegram-blocked-or-a-proxy).
6. Run `check_setup.bat`, then read `logs\assistant.log`.

## "Unauthorized", or Telegram refused the token

If the window says **Telegram rejected the bot token** (exit code 4), the token is wrong, revoked, or was copied with extra characters. Get a fresh token from @BotFather, then follow [Revoke a token and set a new one](#revoke-a-token-and-set-a-new-one).

## Wrong user ID

The bot answers only accounts listed in `allowed_user_ids` in `config.ini`. Find your numeric ID with @userinfobot, put it in that setting (comma-separated for several accounts), and restart `start_assistant.bat`. In the log, a stranger's attempt is a warning with `unauthorised user` and the account's `user_id`. The message text is not logged.

## Python not found, or too old

`install.bat` says which case applies and shows the download page.

* Install **Python 3.11 or newer** from <https://www.python.org/downloads/windows/>.
* On the first installer screen, tick **Add python.exe to PATH**.
* If an old Python is still used, remove it in Windows Settings under **Apps**.
* Open a new window and run `install.bat` again.

## `install.bat` fails

Read the red `[X]` line above the final message. It names the step that failed:

* **Could not create the virtual environment (`.venv`).** The folder must be writable. Move the project out of `Program Files` and run `install.bat` again.
* **The existing `.venv` cannot be used.** It may come from an older Python or was moved. Delete the `.venv` folder next to `install.bat`, then run `install.bat` again.
* **Installing the dependencies failed.** Check the internet connection and run `install.bat` again. The error details are shown above the message.

## Antivirus blocks something

Some antivirus programs quarantine the Python packages in `.venv`, the downloaded `cloudflared.exe`, or the PowerShell calls the bot makes. Look in the antivirus quarantine list and restore the item only if you trust this project. Do not switch the antivirus off to make the bot work. If the policy on your PC is managed, ask the person who manages it. This project has not been tested against a particular antivirus product.

## The bot stops when the window closes

That is by design. The bot is a console program. Keep the `start_assistant.bat` window open, or press Ctrl+C in it to stop the bot. The bot does not add itself to Windows startup, so it does not run after a restart until you start `start_assistant.bat` again.

## Telegram blocked or a proxy

The bot must reach `https://api.telegram.org`. If a firewall or a company network blocks it, the bot retries for about 30 seconds and then stops with a fatal error. The bot uses the standard `HTTPS_PROXY` and `HTTP_PROXY` environment variables when they are set. There is no proxy setting in `config.ini`. Proxy use is not tested on every network. If it fails behind a proxy, check the variables in the window you start the bot from.

## Approvals expire

A plan waits for approval for **15 minutes**. After that the code no longer works, and the bot replies that the plan expired or was already used. Send the request again to get a new plan. A code works only once, and only for the plan it was issued for.

## `/remote` link not working

* **403 on the link.** The link was already used, has expired, or the session has stopped. Send `/remote start` again.
* **The session does not open.** Check the internet connection and try again. Run `main.py --check` in the project folder: it shows whether `cloudflared` is downloaded and whether `ssh` is available. `cloudflared` is downloaded on the first `/remote start`.

## `/install` fails, or the UAC prompt was not clicked

* `/install` and `/uninstall` work only for the apps in the built-in allow-list, and only after you approve the plan.
* The UAC prompt appears **on the PC's screen**, not in Telegram. Click **Yes** there. The bot never clicks it and never bypasses it. If you do not click it, the install does not happen.
* If the bot says winget is not available, install or update **App Installer** from the Microsoft Store, then try again.
* Apps sold only in the Microsoft Store are not supported.

## Revoke a token and set a new one

1. In Telegram, open @BotFather, send `/mybots`, choose your bot, then **API Token** and **Revoke current token**.
2. Copy the new token.
3. Run **`set_token.bat`** in the project folder and paste the token. The input is hidden.
4. **If you have a `token.txt` file, update it too.** The bot reads `token.txt` before the saved token, so an old token in that file keeps being used. Put only the new token in it, on one line, or delete the file.
5. Start `start_assistant.bat` again.

The order the bot uses is: `token.txt`, then the `TELEGRAM_BOT_TOKEN` environment variable, then the saved token.

## Uninstall

1. Close the `start_assistant.bat` window.
2. Run `.venv\Scripts\python.exe main.py --forget-token` to delete the saved token. Delete `token.txt` if you made one.
3. Revoke the token in @BotFather so it stops working for anyone who copied it.
4. Delete the project folder. Delete `%APPDATA%\TelegramPCAssistant` too, if it exists.
5. Apps installed with `/install` stay installed. Remove them with `/uninstall NAME` or in Windows Settings under **Apps**.

## Where the logs are, and what they contain

* The audit log is `logs\assistant.log` in the project folder.
* The log records events and metadata: start-up, plans, approvals, and errors.
* The log never contains the bot token. The bot removes the token from every line it writes. A test checks the log file for the token.

---

# استكشاف الأخطاء وإصلاحها

حلول قصيرة لأكثر المشكلات شيوعًا. المرجع الكامل في [README.md](../README.md).

## البوت لا يرد

1. هل نافذة `start_assistant.bat` ما زالت مفتوحة؟ البوت يعمل فقط والنافذة مفتوحة. تعرض النافذة سبب التوقف إن توقف.
2. هل أنت في المحادثة الخاصة مع البوت؟ اضغط **Start** هناك وأرسل `/start`. المجموعات يتجاهلها البوت.
3. هل تعمل نسخة أخرى بالتوكن نفسه؟ نسخة واحدة فقط تستقبل الرسائل. أغلق النسخة الأخرى وأعد التشغيل.
4. هل رقمك في `allowed_user_ids`؟ راجع [رقم المستخدم الخاطئ](#رقم-المستخدم-الخاطئ). البوت لا يرد على الحسابات الأخرى، فالغريب لا يرى شيئًا.
5. هل يصل هذا الجهاز إلى `api.telegram.org`؟ راجع [تيليغرام محظور أو بروكسي](#تيليغرام-محظور-أو-بروكسي).
6. شغّل `check_setup.bat` ثم اقرأ `logs\assistant.log`.

## "Unauthorized" أو رفض Telegram للتوكن

إذا ظهرت في النافذة رسالة **Telegram rejected the bot token** (رمز الخروج 4)، فالتوكن غير صحيح أو ألغي أو نُسخ مع أحرف زائدة. احصل على توكن جديد من @BotFather ثم اتبع [إلغاء التوكن وحفظ توكن جديد](#إلغاء-التوكن-وحفظ-توكن-جديد).

## رقم المستخدم الخاطئ

البوت يرد فقط على الحسابات المذكورة في `allowed_user_ids` داخل `config.ini`. احصل على رقمك الرقمي من @userinfobot وضعه في هذا الإعداد (أرقام متعددة مفصولة بفاصلة)، ثم أعد تشغيل `start_assistant.bat`. في السجل تظهر محاولة الغريب كتحذير بالسبب `unauthorised user` ومعرّف الحساب `user_id`. نص الرسالة لا يُسجَّل.

## Python غير موجود أو قديم

يوضح `install.bat` الحالة ويعرض صفحة التنزيل.

* ثبّت **Python 3.11** أو أحدث من <https://www.python.org/downloads/windows/>.
* في أول شاشة من المثبّت، ضع علامة على **Add python.exe to PATH**.
* إذا ظل يُستخدم Python قديم، أزله من إعدادات Windows ثم **Apps**.
* افتح نافذة جديدة وشغّل `install.bat` مرة أخرى.

## فشل `install.bat`

اقرأ السطر الأحمر `[X]` فوق الرسالة الأخيرة. يذكر الخطوة التي فشلت:

* **تعذّر إنشاء البيئة الافتراضية `.venv`.** يجب أن يكون المجلد قابلًا للكتابة. انقل المشروع خارج `Program Files` ثم شغّل `install.bat` مرة أخرى.
* **مجلد `.venv` الموجود لا يمكن استخدامه.** قد يكون من Python قديم أو نُقل. احذف مجلد `.venv` بجانب `install.bat` ثم شغّله مرة أخرى.
* **فشل تثبيت المكتبات.** تحقق من اتصال الإنترنت وشغّل `install.bat` مرة أخرى. تفاصيل الخطأ تظهر فوق الرسالة.

## مضاد الفيروسات يمنع شيئًا

قد يعزل بعض برامج الحماية حزم Python في `.venv` أو ملف `cloudflared.exe` المحمَّل أو أوامر PowerShell التي يشغلها البوت. افتح قائمة العزل في مضاد الفيروسات واسترجع العنصر فقط إذا كنت تثق بهذا المشروع. لا تُطفئ مضاد الفيروسات لتشغيل البوت. إذا كانت الإعدادات على هذا الجهاز تديرها جهة أخرى، فاسأل من يديرها. لم يُختبر هذا المشروع مع منتج مضاد فيروسات بعينه.

## البوت يتوقف عند إغلاق النافذة

هذا مقصود. البوت برنامج يعمل في نافذة الأوامر. أبقِ نافذة `start_assistant.bat` مفتوحة، أو اضغط Ctrl+C فيها لإيقاف البوت. البوت لا يضيف نفسه إلى بدء تشغيل Windows، لذلك لا يعمل بعد إعادة التشغيل حتى تشغّل `start_assistant.bat` من جديد.

## تيليغرام محظور أو بروكسي

يجب أن يصل البوت إلى `https://api.telegram.org`. إذا منعته جدار حماية أو شبكة شركة، يحاول البوت لمدة 30 ثانية تقريبًا ثم يتوقف بخطأ قاتل. يستخدم البوت متغيرات البيئة القياسية `HTTPS_PROXY` و`HTTP_PROXY` إذا كانت مضبوطة. لا يوجد إعداد بروكسي في `config.ini`. لم يُختبر استخدام البروكسي على كل الشبكات. إذا فشل خلف بروكسي، فتحقق من هذه المتغيرات في النافذة التي تشغّل البوت منها.

## الموافقات تنتهي

تنتظر الخطة الموافقة **15 دقيقة**. بعدها لا يعمل الرمز، ويرد البوت بأن الخطة انتهت أو استُخدمت. أرسل الطلب من جديد للحصول على خطة جديدة. الرمز يعمل مرة واحدة فقط، ولخطة واحدة فقط.

## رابط `/remote` لا يعمل

* **403 على الرابط.** الرابط استُخدم أو انتهت صلاحيته أو توقفت الجلسة. أرسل `/remote start` من جديد.
* **الجلسة لا تفتح.** تحقق من اتصال الإنترنت وأعد المحاولة. شغّل `main.py --check` في مجلد المشروع: يبيّن هل نُزّل `cloudflared` وهل `ssh` متاح. يُنزَّل `cloudflared` عند أول `/remote start`.

## فشل `/install` أو لم يُضغط مربع UAC

* يعمل `/install` و`/uninstall` فقط للتطبيقات الموجودة في القائمة المسموحة المدمجة، وبعد موافقتك على الخطة.
* يظهر مربع **UAC** **على شاشة الجهاز**، وليس في تيليغرام. اضغط **Yes** هناك. البوت لا يضغطه ولا يتجاوزه. إذا لم تضغطه فلن يتم التثبيت.
* إذا قال البوت إن winget غير متاح، فثبّت **App Installer** أو حدّثه من Microsoft Store ثم أعد المحاولة.
* التطبيقات التي تُباع في متجر Microsoft فقط غير مدعومة.

## إلغاء التوكن وحفظ توكن جديد

1. في تيليغرام، افتح @BotFather وأرسل `/mybots`، ثم اختر البوت، ثم **API Token** ثم **Revoke current token**.
2. انسخ التوكن الجديد.
3. شغّل **`set_token.bat`** في مجلد المشروع والصق التوكن. الإدخال مخفي.
4. **إذا كان عندك ملف `token.txt`، فحدّثه أيضًا.** يقرأ البوت `token.txt` قبل التوكن المحفوظ، لذلك يستمر التوكن القديم في الملف بالعمل. ضع فيه التوكن الجديد فقط في سطر واحد، أو احذف الملف.
5. شغّل `start_assistant.bat` مرة أخرى.

ترتيب القراءة في البوت: `token.txt`، ثم متغير البيئة `TELEGRAM_BOT_TOKEN`، ثم التوكن المحفوظ.

## الإزالة

1. أغلق نافذة `start_assistant.bat`.
2. شغّل `.venv\Scripts\python.exe main.py --forget-token` لحذف التوكن المحفوظ. واحذف `token.txt` إن أنشأته.
3. ألغِ التوكن في @BotFather حتى لا يعمل لأي شخص نسخه.
4. احذف مجلد المشروع. واحذف `%APPDATA%\TelegramPCAssistant` أيضًا إن وُجد.
5. التطبيقات التي ثُبتت عبر `/install` تبقى مثبتة. أزلها بـ `/uninstall NAME` أو من إعدادات Windows ثم **Apps**.

## مكان السجلات وما تحويه

* سجل التدقيق هو `logs\assistant.log` داخل مجلد المشروع.
* يسجّل السجل الأحداث والبيانات الوصفية: بدء التشغيل، والخطط، والموافقات، والأخطاء.
* السجل لا يحوي التوكن أبدًا. يزيل البوت التوكن من كل سطر يكتبه، ويفحص أحد الاختبارات ملف السجل بحثًا عن التوكن.
