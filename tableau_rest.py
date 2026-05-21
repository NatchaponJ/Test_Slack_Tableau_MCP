import requests
import xml.etree.ElementTree as ET


class TableauCloudClient:

    def __init__(self, server_url: str, site_id: str, token_name: str, token_value: str):
        self.server_url = server_url.rstrip("/")
        self.site_id = site_id
        self.token_name = token_name
        self.token_value = token_value
        self.auth_token = None
        self.site_luid = None
        self.api_version = "3.21"

    def login(self):
        url = f"{self.server_url}/api/{self.api_version}/auth/signin"
        payload = {
            "credentials": {
                "personalAccessTokenName": self.token_name,
                "personalAccessTokenSecret": self.token_value,
                "site": {"contentUrl": self.site_id}
            }
        }
        response = requests.post(url, json=payload)
        response.raise_for_status()

        ns = {"t": "http://tableau.com/api"}
        root = ET.fromstring(response.text)
        credentials = root.find("t:credentials", ns)
        self.auth_token = credentials.get("token")
        self.site_luid = credentials.find("t:site", ns).get("id")
        print(f">>> Tableau login สำเร็จ | site_luid: {self.site_luid}")

    def logout(self):
        if not self.auth_token:
            return
        url = f"{self.server_url}/api/{self.api_version}/auth/signout"
        requests.post(url, headers=self._auth_headers())
        self.auth_token = None

    def _auth_headers(self) -> dict:
        return {
            "x-tableau-auth": self.auth_token,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def query_datasource(self, datasource_luid: str, query: dict) -> list:
        """
        query คือ dict แบบ VizQL format:
        {
            "fields": [{"fieldCaption": "Product Name"}, {"fieldCaption": "Sales", "function": "SUM"}],
            "filters": []
        }
        ไม่ใช่ SQL string แล้ว
        """
        if not self.auth_token:
            self.login()

        # endpoint จาก official docs
        url = f"{self.server_url}/api/v1/vizql-data-service/query-datasource"

        # body ต้องมี datasource object ครอบด้วย
        payload = {
            "datasource": {
                "datasourceLuid": datasource_luid
            },
            "query": query,
            "options": {
                "returnFormat": "OBJECTS"  # ให้ตอบกลับเป็น list of dict
            }
        }

        response = requests.post(url, headers=self._auth_headers(), json=payload)
        print(f">>> VizQL status: {response.status_code}")
        print(f">>> VizQL body: {response.text[:500]}")

        if response.status_code == 401:
            print(">>> Token หมดอายุ กำลัง re-login...")
            self.login()
            response = requests.post(url, headers=self._auth_headers(), json=payload)

        response.raise_for_status()
        data = response.json()

        # response format: {"data": [{...}, {...}]}
        return data.get("data", [])


# ------------------------------------------------------------------ #
#  ฟังก์ชันหลัก — รับ query dict แทน SQL string
# ------------------------------------------------------------------ #

def query_tableau_cloud(query: dict, datasource_luid: str, client: TableauCloudClient) -> list:
    """
    query คือ VizQL format dict เช่น:
    {
        "fields": [
            {"fieldCaption": "Product Name"},
            {"fieldCaption": "Sales", "function": "SUM", "sortDirection": "DESC", "sortPriority": 1}
        ],
        "filters": []
    }
    """
    try:
        results = client.query_datasource(datasource_luid, query)
        print(f">>> ได้ข้อมูล {len(results)} แถว")
        return results
    except requests.HTTPError as e:
        print(f">>> Tableau API error: {e.response.status_code} | {e.response.text}")
        raise
    except Exception as e:
        print(f">>> query error: {e}")
        raise
