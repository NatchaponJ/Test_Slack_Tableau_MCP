import requests
import json


class TableauCloudClient:
    """
    จัดการการเชื่อมต่อกับ Tableau Cloud ทั้งหมด
    - Login / logout อัตโนมัติ
    - Query ข้อมูลผ่าน VizQL Data Service API
    - รองรับทั้ง Extract และ Live datasource
    """

    def __init__(self, server_url: str, site_id: str, token_name: str, token_value: str):
        self.server_url = server_url.rstrip("/")
        self.site_id = site_id
        self.token_name = token_name
        self.token_value = token_value

        # จะถูก set หลัง login สำเร็จ
        self.auth_token = None
        self.site_luid = None
        self.api_version = "3.21"

    # ------------------------------------------------------------------ #
    #  Authentication
    # ------------------------------------------------------------------ #

    def login(self):
        """
        Login ด้วย Personal Access Token (PAT)
        เก็บ auth_token และ site_luid ไว้ใช้กับ request ถัดไป
        """
        url = f"{self.server_url}/api/{self.api_version}/auth/signin"

        payload = {
            "credentials": {
                "personalAccessTokenName": self.token_name,
                "personalAccessTokenSecret": self.token_value,
                "site": {"contentUrl": self.site_id}
            }
        }

        response = requests.post(url, json=payload)
        response.raise_for_status()  # throw ถ้า HTTP error

        data = response.json()
        self.auth_token = data["credentials"]["token"]
        self.site_luid = data["credentials"]["site"]["id"]
        print(f">>> Tableau login สำเร็จ | site_luid: {self.site_luid}")

    def logout(self):
        """Sign out เพื่อ invalidate token ที่ใช้แล้ว"""
        if not self.auth_token:
            return

        url = f"{self.server_url}/api/{self.api_version}/auth/signout"
        requests.post(url, headers=self._auth_headers())
        self.auth_token = None
        print(">>> Tableau logout แล้ว")

    def _auth_headers(self) -> dict:
        """Header มาตรฐานที่ทุก request ต้องใส่"""
        return {
            "x-tableau-auth": self.auth_token,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    # ------------------------------------------------------------------ #
    #  Query ข้อมูล
    # ------------------------------------------------------------------ #

    def query_datasource(self, datasource_luid: str, sql: str) -> list[dict]:
        """
        ส่ง SQL ไปยัง Tableau Cloud แล้วได้ผลลัพธ์กลับมาเป็น list of dict

        Tableau ใช้ VizQL Data Service ซึ่งรับ SQL-like query
        รองรับทั้ง Extract และ Live connection โดยอัตโนมัติ

        Args:
            datasource_luid: ID ของ datasource (ดูได้จาก Tableau Cloud URL)
            sql: SQL query string (ใช้ syntax เดิมได้เลย)

        Returns:
            เช่น [{"Product Name": "Apple", "Sales": 500}, ...]
        """
        if not self.auth_token:
            self.login()

        url = (
            f"{self.server_url}/api/{self.api_version}"
            f"/sites/{self.site_luid}/datasources/{datasource_luid}/data"
        )

        payload = {"query": sql}

        response = requests.post(url, headers=self._auth_headers(), json=payload)

        # ถ้า token หมดอายุ ให้ login ใหม่แล้วลองอีกครั้ง
        if response.status_code == 401:
            print(">>> Token หมดอายุ กำลัง re-login...")
            self.login()
            response = requests.post(url, headers=self._auth_headers(), json=payload)

        response.raise_for_status()
        data = response.json()

        # แปลง Tableau response format → list of dict
        return self._parse_response(data)

    def _parse_response(self, data: dict) -> list[dict]:
        """
        Tableau ตอบกลับในรูปแบบนี้:
        {
            "data": {
                "columns": [{"name": "Product Name"}, {"name": "Sales"}],
                "rows": [["Apple", 500], ["Banana", 300]]
            }
        }
        แปลงให้เป็น [{"Product Name": "Apple", "Sales": 500}, ...]
        """
        try:
            columns = [col["name"] for col in data["data"]["columns"]]
            rows = data["data"]["rows"]
            return [dict(zip(columns, row)) for row in rows]
        except KeyError as e:
            print(f">>> parse response error: {e} | raw: {data}")
            return []


# ------------------------------------------------------------------ #
#  ฟังก์ชันหลักที่ใช้แทน query_with_sql เดิม
# ------------------------------------------------------------------ #

def query_tableau_cloud(sql: str, datasource_luid: str, client: TableauCloudClient) -> list[dict]:
    """
    ใช้แทน query_with_sql(sql, hyper_path) เดิมได้เลย

    ต่างกันตรงที่:
    - ไม่ต้องมีไฟล์ .hyper ในเครื่อง
    - ดึงข้อมูลจาก Tableau Cloud โดยตรง
    - ถ้า datasource เป็น Live จะได้ข้อมูลสดทันที
    - ถ้าเป็น Extract จะได้ข้อมูลล่าสุดที่ refresh ไว้

    Args:
        sql: SQL query (ใช้ syntax เดิมได้เลย)
        datasource_luid: TABLEAU_DATASOURCE_ID จาก .env
        client: TableauCloudClient instance

    Returns:
        list of dict เหมือนเดิมเลย
    """
    try:
        results = client.query_datasource(datasource_luid, sql)
        print(f">>> ได้ข้อมูล {len(results)} แถว")
        return results
    except requests.HTTPError as e:
        print(f">>> Tableau API error: {e.response.status_code} | {e.response.text}")
        raise
    except Exception as e:
        print(f">>> query error: {e}")
        raise
