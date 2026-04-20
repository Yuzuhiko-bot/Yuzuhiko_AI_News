import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# 必要なスコープ（ドキュメントの編集とドライブへのファイル作成）
SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive.file'
]

def main():
    creds_path = 'credentials.json'
    
    if not os.path.exists(creds_path):
        # 隣のディレクトリも探してみる
        sibling_path = os.path.join('..', 'gmail-organizer', 'credentials.json')
        if os.path.exists(sibling_path):
            creds_path = sibling_path
        else:
            print("エラー: credentials.json が見つかりません。")
            print("Google Cloud Console からダウンロードして、このディレクトリに配置してください。")
            return

    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    creds = flow.run_local_server(port=0)

    # GitHub Secrets に登録するための JSON 形式を生成
    auth_info = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes
    }

    print("\n" + "="*60)
    print("【GitHub Secrets 用のトークン JSON】")
    print("以下の文字列をすべてコピーして、GitHub Secrets の 'GOOGLE_USER_CREDENTIALS' に登録してください。")
    print("="*60 + "\n")
    print(json.dumps(auth_info, indent=2))
    print("\n" + "="*60)

if __name__ == '__main__':
    main()
