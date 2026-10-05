# iCloud Mail Assistant

A small local tool for cleaning up and managing an iCloud mailbox.

---

## English

### What it does

iCloud Mail Assistant helps you review newsletters and bulk email from your iCloud inbox.

Current features:

- Scan your iCloud inbox locally
- Group similar senders by company
- Select one or multiple companies at once
- Unsubscribe from mailing lists when `List-Unsubscribe` is available
- Delete old emails without unsubscribing
- Unsubscribe without deleting
- Unsubscribe and delete in one action
- Choose whether to delete only advertising-like emails or all emails from selected companies
- Review individual emails before deletion
- Untick any email you want to keep
- Move deleted emails to the iCloud Trash instead of permanently deleting them
- Save the last scan locally so you do not need to scan the whole inbox every time
- Russian / English interface
- Your main Apple Account password is never required

### How to sign in

You need:

1. Your iCloud email address
2. An Apple **app-specific password**

Do **not** use your normal Apple Account password.

To create an app-specific password:

1. Open `https://account.apple.com`
2. Sign in
3. Open **Sign-In and Security**
4. Choose **App-Specific Passwords**
5. Create a new password, for example: `Mail Assistant`
6. Copy the generated password
7. Enter your iCloud email and this app-specific password in iCloud Mail Assistant

The password is used only to connect to iCloud Mail via IMAP.

### How to start the app

1. Open the folder containing the app
2. Double-click `START.bat`
3. Wait for the browser window to open
4. Enter your iCloud email and app-specific password
5. Click **Scan**

After the first scan, you can usually use **Load previous scan** instead of scanning the whole mailbox again.

### Safety

- The app does not need your main Apple password
- The app-specific password can be revoked at any time from your Apple Account
- Deleted emails are moved to the iCloud Trash first
- Before deletion, you can review the exact messages and deselect anything you want to keep
- The app runs locally on your Windows computer

---

## Русский

### Что умеет приложение

iCloud Mail Assistant помогает разбирать рассылки и массовые письма в iCloud Mail.

Текущие функции:

- Локально сканирует входящие письма iCloud
- Объединяет похожих отправителей по компаниям
- Позволяет выбрать одну или сразу несколько компаний
- Отписывается от рассылок, если отправитель поддерживает `List-Unsubscribe`
- Может удалить старые письма без отписки
- Может отписаться без удаления
- Может одновременно отписаться и удалить старые письма
- Можно выбрать: удалить только письма, похожие на рекламу, или все письма выбранной компании
- Перед удалением показывает конкретные письма
- Можно снять галочку с любого письма, которое нужно оставить
- Письма перемещаются в Корзину iCloud, а не удаляются навсегда сразу
- Сохраняет последнее сканирование локально, чтобы не сканировать всю почту каждый раз
- Интерфейс на русском и английском
- Основной пароль Apple Account не нужен

### Как войти

Нужно:

1. Ваш адрес iCloud Mail
2. Отдельный **пароль приложения Apple**

Не используйте обычный пароль от Apple Account.

Как создать пароль приложения:

1. Откройте `https://account.apple.com`
2. Войдите в аккаунт
3. Откройте **Вход и безопасность / Sign-In and Security**
4. Выберите **Пароли приложений / App-Specific Passwords**
5. Создайте новый пароль, например с названием `Mail Assistant`
6. Скопируйте сгенерированный пароль
7. Введите свой iCloud email и этот пароль в приложении

Этот пароль используется только для подключения к iCloud Mail через IMAP.

### Как запустить

1. Откройте папку с приложением
2. Дважды нажмите `START.bat`
3. Подождите, пока откроется браузер
4. Введите iCloud email и пароль приложения
5. Нажмите **Просканировать**

После первого сканирования обычно можно нажимать **Загрузить прошлое сканирование**, чтобы не ждать повторного полного сканирования.

### Безопасность

- Основной пароль Apple не используется
- Пароль приложения можно в любой момент отозвать в настройках Apple Account
- Удаляемые письма сначала отправляются в Корзину iCloud
- Перед удалением можно посмотреть конкретные письма и снять галочки с тех, которые нужно оставить
- Приложение работает локально на Windows-компьютере
