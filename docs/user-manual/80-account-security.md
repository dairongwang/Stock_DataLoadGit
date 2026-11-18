# Account security: two-factor authentication, connected accounts, password reset

Everything on this page applies to dashboard users (*My account*) and to
portal customers (*Security* page) alike.

## Two-factor authentication (2FA)

With a second factor, signing in needs your password **and** a code. You can
enable several methods; any one of them signs you in.

| Method | How |
|--------|-----|
| **Authenticator app** | *Set up* shows a key (and a QR code if the optional `qrcode` package is installed). Add it in Google Authenticator, Microsoft Authenticator, Authy, 1Password … and confirm with the current 6-digit code. Works offline. |
| **E-mail code** | *Send code* mails a 6-digit code to your (verified) e-mail address; enter it to activate. At sign-in press *Send a code*. |
| **Text message (SMS)** | enter your phone number in international format (`+1 555 010 1234`), confirm the code that arrives. |

When you enable your first method you receive **ten recovery codes** - each
signs you in once if you cannot use your phone. Store them safely; they are
shown only then. *Generate new codes* (with your password) replaces them.

Codes are valid 10 minutes, may be tried 5 times and can be re-sent after
60 seconds. Removing a method needs your password. **Portal administrators
must have a second factor** before the admin pages open.

Locked out? An administrator can reset your second factor (dashboard:
*Users* → *Reset 2FA*; portal: *Admin* → *Customers*).

## Connected accounts (Google, Apple, Facebook)

*Connect* on the account / security page links the identity to your
account; afterwards *Continue with …* on the sign-in page signs you in with
one click - your second factor still applies. *Disconnect* removes the
link; the only way into an account cannot be removed - set a password first.

Accounts created through a provider have **no password**. To add one, sign
out and use *Forgot your password*: the link sent to your e-mail address
lets you set it.

## Password reset

*Forgot your password?* on the sign-in page asks for your user name or
e-mail address (portal: e-mail address) and sends a **single-use link**
valid 60 minutes. The answer is the same whether or not the address is
known. Opening the link lets you choose a new password; afterwards sign in
again. Users without an e-mail address ask an administrator to reset the
password.

## Where codes and links arrive

Through the configured mail / SMS provider. With the default **stub**
providers nothing is sent: the message is printed to the **server
console** and the log, and the pages show a "stub mode" notice.
