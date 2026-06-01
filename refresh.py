from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]

flow = InstalledAppFlow.from_client_secrets_file(
  "credentials.json",
  SCOPES,
)

creds = flow.run_local_server(
  port=0,
  access_type="offline",
  prompt="consent",
)

print("ACCESS TOKEN:")
print(creds.token)
print()
print("REFRESH TOKEN:")
print(creds.refresh_token)
