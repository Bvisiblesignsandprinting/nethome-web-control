from getpass import getpass
from app.credentials import save_credentials

print("NetHome Plus credential setup")
account = input("NetHome Plus email: ").strip()
password = getpass("NetHome Plus password: ")
if not account or not password:
    raise SystemExit("Account and password are required.")
save_credentials(account, password)
print("Saved securely using the operating system credential store.")
