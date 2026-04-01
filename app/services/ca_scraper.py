import httpx
from typing import List, Dict
from playwright.async_api import async_playwright

from app.services.registry import register_scraper


BASE_URL = "https://www2.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"


async def _scrape_single_page(page, state_code: str) -> List[Dict]:
    """
    TODO: Implement the real CSLB interaction here:
      - navigate to BASE_URL
      - fill search form (e.g., classification, city, etc.)
      - submit
      - wait for results
      - parse rows into normalized dicts
    """
    # Placeholder structure – you’ll wire in the real selectors.
    licenses: List[Dict] = []

    # Example: await page.goto(BASE_URL)
    # Example: await page.fill("#ctl00_ContentPlaceHolder1_txtContractorLicenseNo", "123456")
    # Example: await page.click("#ctl00_ContentPlaceHolder1_btnSearch")
    # Example: await page.wait_for_selector("table#ctl00_ContentPlaceHolder1_dgSearchResults")

    # Example parsing loop:
    # rows = await page.query_selector_all("table#... tr.dataRow")
    # for row in rows:
    #     lic_no = await (await row.query_selector("td:nth-child(1)")).inner_text()
    #     name = await (await row.query_selector("td:nth-child(2)")).inner_text()
    #     status = await (await row.query_selector("td:nth-child(3)")).inner_text()
    #     licenses.append(
    #         {
    #             "license_number": lic_no.strip(),
    #             "status": status.strip(),
    #             "contractor_name": name.strip(),
    #         }
    #     )

    return licenses


async def fetch_ca_licenses(state_code: str) -> List[Dict]:
    """
    Entry point used by the orchestrator.
    Returns a list of dicts with:
      - license_number
      - status
      - contractor_name
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            licenses = await _scrape_single_page(page, state_code)
        finally:
            await browser.close()

    return licenses


# Register this scraper with the registry
register_scraper("CA", fetch_ca_licenses)
