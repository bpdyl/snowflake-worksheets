import argparse
import datetime
import http.cookiejar
import requests
import json
import logging
import re
import socket
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from logging_config import setup_logging
from variables import Variables
from urllib.parse import urlparse, parse_qs
from typing import Optional


def fake_user_agent() -> str:
    """
    User agent to use when sending requests, this is needed otherwise the default urllib user agent is blocked by Cloudflare,
    which Snowflake uses for their app server.

    I just used mine - Firefox 121, on macOS.

    Returns:
        A user agent string to use for requests.
    """
    return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"


def snowsight_client_app_version() -> int:
    """
    Snowsight uses this as of January 2024 on app.snowflake.com for the CLIENT_APP_VERSION when the
    CLIENT_APP_ID is set to "Snowflake UI".

    Returns:
        An integer representing the current date/time in the format of YYYYMMDDHHMMSS.
    """
    return int(datetime.datetime.now().strftime("%Y%m%d%H%M%S"))


def validate_snowflake_url(account_identifier: str) -> dict:
    """
    Validates a Snowflake account using the account identifier,
    and returns the account name, region and app server URL as a dict.

    Args:
        account_identifier:
            Snowflake account identifier, it's usually `https://<account_identifier>.snowflakecomputing.com`.
            Most of the time it is `<USERNAME>.<ACCOUNT_NAME>`.
            Information about account identifiers can be found at
            https://docs.snowflake.com/en/user-guide/admin-account-identifier

    Returns:
        dict with the response from the Snowflake API

    """
    url = f"https://app.snowflake.com/v0/validate-snowflake-url?url={account_identifier}&isSecondaryAccount=false"
    headers = {"User-Agent": fake_user_agent()}

    logging.debug("[GET REQUEST] URL: %s - Headers: %s", url, headers)

    req = urllib.request.Request(
        url,
        method="GET",
        headers=headers,
    )

    # Perform the request and extracts the account name, region and app server URL.
    try:
        with urllib.request.urlopen(req) as response:
            response_data = json.loads(response.read().decode())
            logging.debug("[RESPONSE] - %s", response_data)

            # account - The account name, e.g. `MYACCOUNT`
            # region - the region of the snowflake account (us-east-1, etc.)
            # instance_url - The URL to the user's instance, usually `https://<ACCOUNT_NAME>.<REGION>.snowflakecomputing.com`
            # app_server_url - the snowsight API URL, `https://apps-api.c1.<REGION>.aws.app.snowflake.com`
            # valid - True if the account is valid, False if it's not.

            return {
                "account": response_data["account"],
                "region": response_data["region"],
                "instance_url": response_data["url"],
                "app_server_url": response_data["appServerUrl"],
                "valid": response_data["valid"],
            }

    except urllib.error.URLError as e:
        logging.error(e.reason)
        print(e.reason)
        sys.exit(1)


def snowsight_bootstrap(
    app_server_url: str,
    instance_url: str,
    name: Optional[str] = None,
    cookies: Optional[str] = None,
) -> dict:
    """
    There are two different bootstrap methods, one for authenticated users, and one for unauthenticated users.

    If performing an unauthenticated request, then the `name` and `cookies` parameters should be None.

    Snowflake requires an `csrfToken` and an ` OrganizationID ` (if authenticated) for interacting with endpoints,
    which you can retrieve from the `bootstrap` endpoint.

    Args:
        app_server_url: The app server URL from validate-url (Usually `'https://apps-api.c1.ap-southeast-2.aws.app.snowflake.com')
        instance_url: The instance URL from validate-url (Usually `https://<ACCOUNT_NAME>.<REGION>.snowflakecomputing.com')
        name: The login name to use, if you're logged in, this is the username, otherwise it's None.
        cookies: The list of cookies to use for authentication

    Returns:
        A dict containing the `csrf_token` and `org_id` (if it's an authenticated request) for the Snowflake account.
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": fake_user_agent(),
    }

    if name is not None and instance_url is not None:
        headers["X-Snowflake-Context"] = f"{name.upper()}::{instance_url}"

    if cookies is not None:
        headers["Cookie"] = cookies

    url = f"{app_server_url}/bootstrap"

    logging.debug("[GET REQUEST] URL: %s Headers: %s", url, headers)

    req = urllib.request.Request(url, headers=headers, method="GET")

    # Perform the request and extract the `csrfToken` and `OrganizationID` (if authenticated).
    try:
        with urllib.request.urlopen(req) as response:
            response_data = json.loads(response.read().decode())
            logging.debug("[RESPONSE] - %s", response_data)

            csrf_token = response_data["PageParams"]["csrfToken"]

            # Unauthenticated responses don't have the org (as they don't have a user, as they're not logged in)
            if response_data.get("User", None) is None:
                logging.debug("Has no user, csrf_token %s", csrf_token)
                return {"csrf_token": csrf_token, "org_id": None}

            # `OrganizationID` is from either:
            #
            # 1. response_data["Org"]["id"].
            # 2. That value can be null/empty, fall back to response_data["User"]["defaultOrgId"]
            org_id = response_data.get("Org", {}).get("id", None) or response_data[
                "User"
            ].get("defaultOrgId", None)

            logging.debug("csrf_token - %s, org_id - %s", csrf_token, org_id)

            return {"csrf_token": csrf_token, "org_id": org_id}

    except urllib.error.URLError as e:
        logging.error(e.reason)
        print(e.reason)
        sys.exit(1)


### OAUTH
def start_oauth(app_server_url: str, instance_url: str, csrf_token: str) -> dict:
    """
    Starts the OAuth flow, returning the redirect URL and cookies.

    Args:
        app_server_url: The app server URL from validate-url (Usually `'https://apps-api.c1.ap-southeast-2.aws.app.snowflake.com')
        instance_url: The instance URL from validate-url (Usually `https://<ACCOUNT_NAME>.<REGION>.snowflakecomputing.com')
        csrf_token: The csrf token from the bootstrap request

    Returns:
        A dict containing the redirect URL and cookies.
    """

    # The state needs to be these values, otherwise the login will fail.
    # Passing additional values will cause the login to fail.
    # If new values are added/removed in future, you'll need to update this.
    state = '{{"csrf":"{0}","url":"{1}","browserUrl":"{2}"}}'.format(
        csrf_token, instance_url, "https://app.snowflake.com/"
    )

    instance_url_encoded = urllib.parse.quote_plus(instance_url)
    state_encoded = urllib.parse.quote_plus(state)
    url = f"{app_server_url}/start-oauth/snowflake?accountUrl={instance_url_encoded}&&state={state_encoded}"

    headers = {
        "User-Agent": fake_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://app.snowflake.com/"
    }

    logging.debug("[GET REQUEST] URL: %s - Headers: %s", url, headers)

    req = urllib.request.Request(url, headers=headers, method="GET")

    # get cookies from req
    cookie_jar = http.cookiejar.CookieJar()

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    # Create an opener that will use the cookie jar
    try:
        with opener.open(req) as response:
            # Open the URL, we don't care about the response, we just want the cookies.
            logging.debug("[RESPONSE] - %s", response.read().decode())

            # Extract cookies from the cookie jar
            cookies_list = []

            for cookie in [cookie for cookie in cookie_jar]:
                cookies_list.append(cookie.name + "=" + cookie.value.replace('"', ""))

            cookies = "; ".join(cookies_list)
            logging.debug("Extracted cookies: %s", cookies)

            final_url = response.url
            parsed_url = urlparse(final_url)
            params = parse_qs(parsed_url.query)
            scope = params["scope"][0]
            client_id = params["client_id"][0]
            response_type = params["response_type"][0]
            code_challenge = params["code_challenge"][0]
            code_challenge_method = params["code_challenge_method"][0]
            redirect_uri = params["redirect_uri"][0]
            returned_state = params["state"][0]
            state_dict = json.loads(returned_state)
            originator = state_dict["originator"]

            return {
                "cookies": cookies,
                "oauth_nonce": state_dict["oauthNonce"],
                "scope": scope,
                "client_id": client_id,
                "response_type": response_type,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "redirect_uri": redirect_uri,
                "originator": originator,
            }

    except urllib.error.URLError as e:
        logging.error(e.reason)
        print(e.reason)
        sys.exit(1)


def build_authenticate_request_payload(
    instance_url: str,
    account_name: str,
    login_name: str,
    oauth_nonce: str,
    csrf: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str,
    client_id: str,
    scope: str,
    redirect_uri: str,
    originator: str,
    password: Optional[str] = None,
    private_key_token: Optional[str] = None,
    inflight_ctx: Optional[str] = None,
    proof_key: Optional[str] = None,
    token: Optional[str] = None,
) -> dict:
    """
    Builds the authenticate request payload, which is sent to the session/authenticate-request endpoint.

    Depending on your authentication method, you will need the following;

    Username/Password authentication: password
    SSO authentication: None - Just the login name
    Duo authentication (pass code): Duo passcode
    Duo authentication (push): None - Just the login name
    Private key authentication: Private key as a string

    Args:
        instance_url: The instance URL from validate-url (Usually `https://<ACCOUNT_NAME>.<REGION>.snowflakecomputing.com')
        account_name: The account name from validate-url (Usually `ACCOUNT_NAME`)
        login_name: The login name to authenticate with, this is usually the username, but can be different for SSO.
        oauth_nonce: The oauth nonce from the bootstrap request
        csrf: The csrf token from the bootstrap request
        response_type: The response type from the `/start-oauth/snowflake` endpoint
        code_challenge: The code challenge from the `/start-oauth/snowflake` endpoint
        code_challenge_method: The code challenge method from the `/start-oauth/snowflake` endpoint
        client_id: The client ID from the `/start-oauth/snowflake` endpoint
        scope: The scope from the `/start-oauth/snowflake` endpoint
        redirect_uri: The redirect URI from the `/start-oauth/snowflake` endpoint
        originator: The originator from the `/start-oauth/snowflake` endpoint
        password: The password to authenticate with, if using username/password authentication.
        private_key_token: The JWT from the private_key to authenticate with, if using private key authentication.
        inflight_ctx: The inflight_ctx from the login-request endpoint, if using Duo authentication.
        proof_key: When logging in via SSO, the proof key returned from the `/session/authenticator-request` endpoint.
        token: When logging in via SSO, the token returned from the `/session/authenticator-request` endpoint.

    Returns:
        Payload to send to the session/authenticate-request endpoint.

    """

    # The state needs to be these values, otherwise the login will fail.
    # Passing additional values will cause the login to fail.
    # If new values are added/removed in future, you'll need to update this.
    state = '{{"csrf":"{0}","url":"{1}","browserUrl":"{2}","originator":"{3}","oauthNonce":"{4}"}}'.format(
        csrf, instance_url, "https://app.snowflake.com/", originator, oauth_nonce
    )

    payload = {
        "data": {
            "ACCOUNT_NAME": account_name.upper(),
            "LOGIN_NAME": login_name,
            "clientId": client_id,
            "redirectUri": redirect_uri,
            "responseType": response_type,
            "state": state,
            "scope": scope,
            "codeChallenge": code_challenge,
            "codeChallengeMethod": code_challenge_method,
            "CLIENT_APP_ID": "Snowflake UI",
            "CLIENT_APP_VERSION": snowsight_client_app_version(),
        }
    }

    if password and not inflight_ctx:
        payload["data"]["PASSWORD"] = password

    if private_key_token:
        # add AUTHENTICATOR
        payload["data"]["AUTHENTICATOR"] = "SNOWFLAKE_JWT"
        # add TOKEN
        payload["data"]["TOKEN"] = private_key_token

    if inflight_ctx:
        payload["inFlightCtx"] = inflight_ctx
        # remove LOGIN_NAME & ACCOUNT_NAME from data, as they're not needed
        payload["data"].pop("LOGIN_NAME", None)
        payload["data"].pop("ACCOUNT_NAME", None)

    if proof_key and token:
        payload["data"]["AUTHENTICATOR"] = "EXTERNALBROWSER"
        payload["data"]["TOKEN"] = token
        payload["data"]["PROOF_KEY"] = proof_key

    return payload


def authenticate_request(instance_url: str, payload: dict) -> str:
    """
    Sends a POST request to the session/authenticate-request endpoint with the login payload, returning the masterToken,
    which is used in the authorization request.

    This function does not perform any error handling, so if the login fails, it will just exit.

    Args:
        instance_url: The instance URL from validate-url (Usually `https://<ACCOUNT_NAME>.<REGION>.snowflakecomputing.com')
        payload: The payload to send to the session/authenticate-request endpoint, built using build_authenticate_request_payload

    Returns:
        The masterToken, which is used in the authorization request.

    """
    data_json = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": fake_user_agent()}

    url = f"{instance_url}/session/authenticate-request?__uiAppName=Login"

    logging.debug(f"[REQUEST] URL: {url} data: {payload} headers: {headers}")

    req = urllib.request.Request(url, data=data_json, headers=headers, method="POST")

    # Perform the request and extract the 'masterToken' from the request, if this
    # doesn't exist, then the login failed - you'll need to implement better error handling.
    try:
        with urllib.request.urlopen(req) as response:
            response_data = json.loads(response.read().decode())
            logging.debug("[RESPONSE] - %s", response_data)
            return response_data["data"]["masterToken"]

    except urllib.error.URLError as e:
        logging.error(e.reason)
        print(e.reason)
        sys.exit(1)


def authorization_request(
    instance_url: str,
    master_token: str,
    oauth_nonce: str,
    csrf: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str,
    client_id: str,
    scope: str,
    redirect_uri: str,
    originator: str,
) -> str:
    """
    Sends a POST request to the oauth/authorization-request endpoint with the masterToken, returning the redirectUrl.

    The oauth/authorization-request endpoint is used to convert the masterToken into a redirectUrl,
    which is then used to complete the OAuth flow.

    Args:
        instance_url: The instance URL from validate-url (Usually `https://<ACCOUNT_NAME>.<REGION>.snowflakecomputing.com')
        master_token: The masterToken from the session/authenticate-request endpoint
        oauth_nonce: The oauth nonce from the bootstrap request
        csrf: The unauthenticated CSRF token from the bootstrap request
        response_type: The response type from the start-oauth request
        code_challenge: The code challenge from the start-oauth request
        code_challenge_method: The code challenge method from the start-oauth request
        client_id: The client ID from the start-oauth request
        scope: The scope from the start-oauth request
        redirect_uri: The redirect URI from the start-oauth request
        originator: The originator from the start-oauth request

    Returns:
        The redirectUrl, which is used to complete the OAuth flow.

    """
    headers = {"Content-Type": "application/json", "User-Agent": fake_user_agent()}

    # The state needs to be these values, otherwise the login will fail.
    # Passing additional values will cause the login to fail.
    # If new values are added/removed in future, you'll need to update this.
    state = '{{"csrf":"{0}","url":"{1}","browserUrl":"{2}","originator":"{3}","oauthNonce":"{4}"}}'.format(
        csrf, instance_url, "https://app.snowflake.com/", originator, oauth_nonce
    )

    url = f"{instance_url}/oauth/authorization-request"
    data = {
        "masterToken": master_token,
        "clientId": client_id,
        "redirectUri": redirect_uri,
        "responseType": response_type,
        "state": state,
        "scope": scope,
        "codeChallenge": code_challenge,
        "codeChallengeMethod": code_challenge_method,
    }
    data_json = json.dumps(data).encode("utf-8")

    logging.debug(
        "authorization_request - URL: %s data: %s headers: %s", url, data, headers
    )

    req = urllib.request.Request(url, data=data_json, headers=headers, method="POST")

    # Perform the request and extract the 'redirectURI'.
    try:
        with urllib.request.urlopen(req) as response:
            response_data = json.loads(response.read().decode())
            logging.debug("authorization_request - %s", response_data)
            return response_data["data"]["redirectUrl"]

    except urllib.error.URLError as e:
        logging.error(e.reason)
        print(e.reason)
        sys.exit(1)


def complete_oauth(
    redirect_url: str, csrf: str, instance_url: str, oauth_nonce: str, cookies: str
) -> str:
    """
    Completes the OAuth flow, returning the cookies as a string that can be used to authenticate with Snowsight.

    We need to get the `S8_SESSION_` and `user-` cookies for future requests.

    Args:
        redirect_url: The redirect URL returned from the login request
        csrf: The csrf token from the bootstrap request
        instance_url: The instance URL from the bootstrap request
        oauth_nonce: The oauth nonce from the bootstrap request
        cookies: The cookies from the bootstrap request

    Returns:
        Cookies, as a string, to use for authentication in Snowsight, prefixed as `S8_SESSION_` and `user-`.
    """

    headers = {
        "Content-Type": "application/json",
        "User-Agent": fake_user_agent(),
        "Cookie": cookies,
    }

    req = urllib.request.Request(redirect_url, headers=headers, method="GET")

    # get cookies from req
    cookie_jar = http.cookiejar.CookieJar()

    # Create an opener that will use the cookie jar
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    logging.debug("complete_oauth [REQUEST] URL: %s headers: %s", redirect_url, headers)

    try:
        with opener.open(req) as response:
            # It's HTML, not JSON
            html_response = response.read().decode()
            logging.debug("complete_oauth [RESPONSE] - %s", html_response)
            cookies_list = []

            for cookie in [cookie for cookie in cookie_jar]:
                cookies_list.append(cookie.name + "=" + cookie.value.replace('"', ""))

            snowsight_authed_cookies = "; ".join(cookies_list)

            logging.debug("complete_oauth - cookies: %s", snowsight_authed_cookies)

            # Snowflake returns initial bootstrap data in the HTML, but it's not JSON, so we can't parse it directly...
            # You could use regex to extract the data, but it's not ideal, as a change of the HTML could break this.
            # We don't really need the information from this anyway, as we get the login name from the cookies.
            # account_params = re.search(r"var params = (.*?);", html_response).group(1)
            # account_params_dict = json.loads(account_params)

            return snowsight_authed_cookies

    except urllib.error.URLError as e:
        logging.error(e.reason)
        print(e.reason)
        sys.exit(1)


def login_request(base_url: str, login_payload: dict) -> dict:
    """
    For Duo passcode authentication (https://duo.com/product/multi-factor-authentication-mfa/authentication-methods/tokens-and-passcodes),
    a user can generate a passcode, valid for a single login.

    This returns a inFlightCtx, which we need to send again to the `login-request` endpoint.

    For Duo Push Notifications, the `/session/v1/login-request` endpoint needs to be required first, as
    this endpoint sends a Push notification to the Duo app on the users phone.

    Once the user has approved the login, a `masterToken` is returned, which is valid for 1 hour.

    This masterToken can then be used with the `oauth/authorization-request` endpoint, bypassing the
    `session/authenticate-request` endpoint, as this endpoint acts as the authentication method.

    Args:
        base_url: The base URL to authenticate to
        login_payload: The login payload, a Python dict.

    Returns: A dict containing the redirect_uri and the name for the header.

    """
    # Convert the data to JSON
    data_json = json.dumps(login_payload).encode("utf-8")
    # Headers
    headers = {
        "Content-Type": "application/json",
        "User-Agent": fake_user_agent(),
    }

    # Create a connection - replace with proxy details if needed
    url = f"{base_url}/session/v1/login-request?__uiAppName=Login"

    logging.debug(
        "[POST REQUEST] URL: %s - Headers: %s - Body: %s", url, headers, login_payload
    )

    req = urllib.request.Request(url, data=data_json, headers=headers, method="POST")

    # Perform the request and extract the 'redirectURI' and userName
    try:
        with urllib.request.urlopen(req) as response:
            response_data = json.loads(response.read().decode())
            logging.debug("[RESPONSE] - %s", response_data)

            # check if ["data"]["nextAction"] is EXT_AUTHN_DUO_BEYOND, if so return the inFlightCtx
            next_action = response_data["data"].get("nextAction", None)
            inflight_ctx = response_data["data"].get("inFlightCtx", None)
            # https://docs.snowflake.com/en/user-guide/security-mfa#mfa-error-codes
            # Check response for code 390128 (EXT_AUTHN_SUCCEEDED)
            if (
                response_data.get("code", None) == "390128"
                and next_action == "EXT_AUTHN_DUO_BEYOND"
                and inflight_ctx is not None
            ):
                return {
                    "redirect_uri": None,
                    "name": None,
                    "master_token": None,
                    "inFlightCtx": response_data["data"]["inFlightCtx"],
                }

            elif response_data["data"].get("redirectURI", None) is not None:
                return {
                    "redirect_uri": response_data["data"]["redirectURI"],
                    "name": response_data["data"]["authnEvent"]["userName"],
                    "inFlightCtx": None,
                    "master_token": None,
                }

            elif response_data["data"].get("masterToken", None) is not None:
                return {
                    "redirect_uri": None,
                    "name": None,
                    "inFlightCtx": None,
                    "master_token": response_data["data"]["masterToken"],
                }

            # Snowflake returns success false if the login fails, but also returns false when authenticating with Duo,
            # so we need to check the code due to this "bug"?
            elif response_data.get("success") is False:
                return_message = "Authentication Failed - "
                # is there a code?
                if response_data.get("code"):
                    return_message += response_data["code"] + " - "

                # is there a message?
                if response_data.get("message"):
                    return_message += response_data["message"] + " - "

                # raise
                raise Exception(return_message)

            if response_data.get("data") is None:
                raise Exception("Authentication Failed - No data returned")

            # nothing found?
            raise Exception("Authentication Failed - No redirectURI returned")

    except urllib.error.URLError as e:
        logging.info(e.reason)


# Authenticated Endpoints (Internal Snowsight API)
def snowsight_entities(
    app_server_url: str,
    instance_url: str,
    name: str,
    org_id: str,
    csrf_token: str,
    cookies: str,
) -> list:
    """
    Returns a list of worksheets for a Snowflake account.

    This example does not perform pagination, so if you have more than 500 worksheets, you will need to
    implement that yourself.

    Args:
        app_server_url: The app server URL from validate-url (Usually `'https://apps-api.c1.ap-southeast-2.aws.app.snowflake.com')
        instance_url: The instance URL from validate-url (Usually `https://<ACCOUNT_NAME>.<REGION>.snowflakecomputing.com')
        name: The login name to use, if you're logged in, this is the username, otherwise it's None.
        org_id: The org_id returned from the bootstrap request.
        csrf_token: The authenticated csrf_token
        cookies: The cookies to use for authentication, returned from complete-oauth

    Returns:
        A list of worksheets for the Snowflake account.
    """

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": csrf_token,
        "X-Snowflake-Context": f"{name.upper()}::{instance_url}",
        "Cookie": cookies,
        "User-Agent": fake_user_agent(),
    }

    url = f"{app_server_url}/v0/organizations/{org_id}/entities/list"
    post_data = "options=%7B%22sort%22%3A%7B%22col%22%3A%22modified%22%2C%22dir%22%3A%22desc%22%7D%2C%22limit%22%3A500%2C%22owner%22%3Anull%2C%22types%22%3A%5B%22query%22%5D%2C%22showNeverViewed%22%3A%22if-invited%22%7D&location=worksheets".encode(
        "utf-8"
    )

    logging.debug(
        "[POST REQUEST] URL: %s - Headers: %s - Body: %s", url, headers, post_data
    )

    # Perform the request and extract all queries.
    try:
        response = requests.post(url, headers=headers, data=post_data, stream=True)

        # Check for successful response
        if response.status_code != 200:
            logging.error(f"Error: {response.status_code}")
            return

        # Process the response data chunk by chunk
        response_data = b''
        total_read_bytes = 0
        content_length = len(response.content)
        logging.info(f'Response Size in MB: {content_length / 1024 / 1024} MB')
        for chunk in response.iter_content(1024 * 1024):  # Adjust chunk size as needed
            chunk_size = len(chunk)
            total_read_bytes += chunk_size
            logging.info(f"Read chunk of size: {chunk_size/1024/1024} MB")
            logging.info(f"Total bytes read: {total_read_bytes / 1024 / 1024} MB")
            remaining_bytes = content_length - total_read_bytes
            logging.info(f"Approximate remaining bytes: {remaining_bytes / 1024 / 1024} MB")
            if not chunk:
                break
            response_data += chunk
        response_data = json.loads(response_data.decode())
        logging.debug("[RESPONSE] - %s", response_data)
        logging.info(f'''Found {len(response_data["models"]["queries"])} queries''')

        
        worksheet_data = {}
        # Iterate through the entities in the "entities" list
        for entity in response_data["entities"]:
        # Extract worksheet_name (assuming it's the same as name)
            worksheet_name = entity["info"]["name"]
            # Extract folder_name
            folder_name = entity["info"]["folderName"]
            # Extract query_language
            query_language = entity["info"]["queryLanguage"]
            # Get query value from the corresponding entity in "models" dictionary
            query_entity_id = entity["entityId"]
            query_details = response_data["models"]["queries"].get(query_entity_id)
            # logging.info(query_details)
            
            if query_details:
                if 'query' in query_details:
                    # If 'query' key exists directly in query_details
                    query_value = query_details['query']
                else:
                    # If 'query' key is inside the 'drafts' dictionary
                    draft_id = next(iter(query_details.get('drafts', {})), None)
                    if draft_id:
                        query_value = query_details['drafts'][draft_id]['query']
                    else:
                        # Handle the case where 'drafts' is empty or not present
                        query_value = None
                if query_value is None:
                    logging.info(query_details)
                    logging.info(f'This is error query: {query_entity_id} and {query_details["url"]}')
                query_url = query_details['url']
            else:
                query_value = None  # Handle missing query entity
                query_url = None
            # Create a dictionary to store the extracted information for this entity
            entity_data = {
                "worksheet_name": worksheet_name,
                "folder_name": folder_name,
                "query_language": query_language,
                "query_value": query_value,
                "worksheet_url": query_url
            }

            # Add the entity data to the worksheet_data dictionary
            worksheet_data[query_entity_id] = entity_data
            
        return worksheet_data
        # queries = []

        # for q in response_data["models"].get("queries", {}).values():
        #     queries.append(q)

        # return queries

    except urllib.error.URLError as e:
        logging.error(e.reason)
        print(e.reason)
        sys.exit(1)


def sso_authenticator_request(
    instance_url: str, account_name: str, login_name: str
) -> dict:
    """
    When logging in with SSO, a local web server is started on a random port, which listens for a redirect from the IdP.

    A request is sent to the `/session/authenticator-request` endpoint, which returns a ssoUrl and proofKey, which the
    user then opens the provided ssoUrl in their browser and authenticates with their IdP.

    Once this has been done, the IdP then makes a POST request to the `fed/login` endpoint with the SAML response,
    which Snowflake then validates. The user is then redirected back to the local web server, with the URL
    being in the url - `http://localhost:XXX/?token=ABC`.

    We then extract the token from the URL, which is passed to `session/authenticate-request`, with the
    `TOKEN` as the returned token, `PROOF_KEY` as the proof key from the initial step (to ensure it's a valid request),
    and `AUTHENTICATOR` set to `EXTERNALBROWSER`.

    Args:
        instance_url: The instance_url returned from validate-url
        account_name: The Snowflake account name, without the region.
        login_name: The login name to authenticate with, this must be the LOGIN_NAME of the user in Snowflake,
                  and the email address of the user in the IdP.

    Returns:
        Dict of token, proof_key - The proof key is used in the final step.
    """

    # First, we need to listen on localhost:{PORT} for the redirect from the IdP. The port doesn't
    # really matter, we can try and bind on a dynamic port.
    # Create a socket and bind to a random port
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("localhost", 0))

    # Accept connections, we don't care about backlog.
    server_socket.listen(0)

    # The callback port is needed, as we need to pass it to Snowflake in the login request (BROWSER_MODE_REDIRECT_PORT).
    callback_port = server_socket.getsockname()[1]
    logging.debug("Listening on http://localhost:%s", callback_port)

    authenticator_request_payload = {
        "data": {
            "CLIENT_APP_ID": "Snowflake UI",
            "CLIENT_APP_VERSION": snowsight_client_app_version(),
            "ACCOUNT_NAME": account_name.upper(),
            "LOGIN_NAME": login_name,
            "AUTHENTICATOR": "EXTERNALBROWSER",
            "BROWSER_MODE_REDIRECT_PORT": str(callback_port),
        }
    }

    data_json = json.dumps(authenticator_request_payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": fake_user_agent(),
    }

    url = f"{instance_url}/session/authenticator-request?__uiAppName=Login"

    logging.debug(
        "[POST REQUEST] URL: %s - Headers: %s - Body: %s",
        url,
        headers,
        authenticator_request_payload,
    )

    req = urllib.request.Request(url, data=data_json, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as response:
            response_data = json.loads(response.read().decode())

            logging.debug("[RESPONSE] - %s", response_data)

            # SSO URL is the URL to redirect the user to, so they can authenticate with their IdP.
            # print this for the user to open it in their browser, as the default browser might be wrong.
            sso_url = response_data["data"]["ssoUrl"]

            # The proof key is used to verify the response from the IdP, we need to store this for later.
            proof_key = response_data["data"]["proofKey"]

            logging.debug("Proof key - %s", proof_key)

            print(
                "Please open the following URL in your browser and authenticate with your IdP."
            )
            print(sso_url)

            # This snippet has been shamelessly borrowed from Snowflakes Python connector, as it's a nice way to handle
            # the redirect from the IdP.
            # [See webbrowser.py](https://github.com/snowflakedb/snowflake-connector-python/blob/main/src/snowflake/connector/auth/webbrowser.py#L117)
            logging.debug("Waiting for redirect from IdP..")

            token = token_socket_listener(server_socket)

            logging.debug("found token for SSO - %s", token)

            return {"token": token, "proof_key": proof_key}

    except urllib.error.URLError as e:
        logging.debug(e.reason)


def token_socket_listener(server_socket: socket) -> str:
    """
    Listens on the provided socket for a redirect from the IdP, returning the token.

    Args:
        server_socket: The socket to listen on.

    Returns:
        The token from the URL, if it exists.

    """
    # This needs to be in a function, so when we have the token we can "return".

    while True:
        socket_client, _ = server_socket.accept()
        try:
            data = socket_client.recv(16384).decode("utf-8").split("\r\n")
            logging.debug("Received data: %s", data)
            # This returns as a list, as it's chunked.
            logging.debug("Finding token..")
            for line in data:
                logging.debug("Line: %s", line)
                if line.startswith("GET /?token="):
                    token = line.split(" ")[1].split("=")[1]
                    logging.info("found token? %s", token)
                    return token
        finally:
            socket_client.shutdown(socket.SHUT_RDWR)
            socket_client.close()


def snowsight_login(
    account_identifier: str,
    login_name: str,
    password: Optional[str] = None
) -> dict:
    """
    Logs into Snowsight using the specified login method.

    Args:
        account_identifier: The account identifier to use, usually `https://<ACCOUNT_NAME>.<REGION>.snowflakecomputing.com`
        login_name: The login name to use, usually the username, but can be different for SSO.
        password: The password to use, if using username/password authentication.

    Returns:
        A dict containing the app_server_url, instance_url, name, org_id, csrf_token and cookies.

    """
    # Validate that the URL is correct.
    logging.info(f"Validating URL for {account_identifier}")
    validated_account_details = validate_snowflake_url(account_identifier)

    valid = validated_account_details["valid"]

    if valid is not True:
        print(f"Account identifier {account_identifier} is not valid")
        sys.exit(1)

    account_name = validated_account_details["account"]
    region = validated_account_details["region"]
    instance_url = validated_account_details["instance_url"]
    app_server_url = validated_account_details["app_server_url"]
    logging.info(
        "Validated account - account: %s, region: %s, instance_url: %s, app_server_url: %s",
        account_name,
        region,
        instance_url,
        app_server_url,
    )
    unauthed_bootstrap = snowsight_bootstrap(
        app_server_url=app_server_url,
        instance_url=instance_url,
        name=None,
        cookies=None,
    )

    unauthed_csrf_token = unauthed_bootstrap["csrf_token"]

    start_oauth_response = start_oauth(
        app_server_url=app_server_url,
        instance_url=instance_url,
        csrf_token=unauthed_csrf_token,
    )

    cookies = start_oauth_response["cookies"]
    scope = start_oauth_response["scope"]
    client_id = start_oauth_response["client_id"]
    response_type = start_oauth_response["response_type"]
    code_challenge = start_oauth_response["code_challenge"]
    code_challenge_method = start_oauth_response["code_challenge_method"]
    redirect_uri = start_oauth_response["redirect_uri"]
    oauth_nonce = start_oauth_response["oauth_nonce"]
    originator = start_oauth_response["originator"]

    # Set when private key authentication is used.
    private_key_token = None

    # Set when duo_passcode authentication is used.
    inflight_ctx = None

    # Set when Duo authentication is used, otherwise we use the master_token from the login request.
    master_token = None

    # When logging in with SSO, these are returned from the `session/authenticator-request` endpoint.
    sso_login_token = None
    sso_login_proof_key = None

    # If we don't have a masterToken by now (e.g. via Duo), we need to authenticate first.
    if master_token is None:
        logging.debug("masterToken is None, authenticating")
        authenticate_payload = build_authenticate_request_payload(
            instance_url,
            account_name=account_name,
            login_name=login_name,
            oauth_nonce=oauth_nonce,
            csrf=unauthed_csrf_token,
            response_type=response_type,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            client_id=client_id,
            scope=scope,
            redirect_uri=redirect_uri,
            originator=originator,
            password=password,
            private_key_token=private_key_token,
            inflight_ctx=inflight_ctx,
            token=sso_login_token,
            proof_key=sso_login_proof_key,
        )

        # Step 1 - Get the masterToken from the session/authenticate-request endpoint
        master_token = authenticate_request(
            instance_url=instance_url, payload=authenticate_payload
        )

    # Step 2 - Pass in the masterToken to the oauth/authorization-request endpoint
    redirect_url = authorization_request(
        instance_url=instance_url,
        master_token=master_token,
        oauth_nonce=oauth_nonce,
        csrf=unauthed_csrf_token,
        response_type=response_type,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        client_id=client_id,
        scope=scope,
        redirect_uri=redirect_uri,
        originator=originator,
    )

    # Step 3 - Complete OAuth
    authed_cookies = complete_oauth(
        # redirect_uri=redirect_uri, account_name=account_name, region=region
        redirect_url=redirect_url,
        csrf=unauthed_csrf_token,
        instance_url=instance_url,
        oauth_nonce=oauth_nonce,
        cookies=cookies,
    )

    # Extracts the login name from the "S8_SESSION_XXX__", as we need this in future steps when authenticating
    # The password in login name might not be the actual username, for example, with SSO.
    # https://docs.snowflake.com/en/sql-reference/functions/all_user_names#usage-notes
    # Usernames (i.e. the NAME property value) are the unique identifier of the user object in Snowflake,
    # while login names (i.e. the LOGIN_NAME property value) are used to authenticate to Snowflake.
    # Usernames are not sensitive data and are returned by other commands and functions (e.g. SHOW GRANTS).
    # Login names are sensitive data.
    authed_name = re.search(r"S8_SESSION_(.*?)__", authed_cookies).group(1)

    logging.info("Bootstrapping with authenticated user - %s", authed_name)
    bootstrap_data = snowsight_bootstrap(
        app_server_url=app_server_url,
        instance_url=instance_url,
        name=authed_name,
        cookies=authed_cookies,
    )

    org_id = bootstrap_data["org_id"]
    authenticated_csrf_token = bootstrap_data["csrf_token"]

    logging.info(
        "Bootstrapped as authenticated user %s, org_id %s", authed_name, org_id
    )

    return {
        "app_server_url": app_server_url,
        "instance_url": instance_url,
        "name": authed_name,
        "org_id": org_id,
        "csrf_token": authenticated_csrf_token,
        "cookies": authed_cookies,
    }


if __name__ == "__main__":
    setup_logging()  
    vars = Variables('ENV.cfg')

    logging.info(f"Account Identifier - {vars.get('ACCOUNT')}")
    logging.info(f"Login name - {vars.get('USERNAME')}")


    login_details = snowsight_login(
        account_identifier = vars.get('ACCOUNT'),
        login_name = vars.get('USERNAME'),
        password = vars.get('PASSWORD'),
    )

    worksheets = snowsight_entities(
        app_server_url=login_details["app_server_url"],
        instance_url=login_details["instance_url"],
        name=login_details["name"],
        org_id=login_details["org_id"],
        csrf_token=login_details["csrf_token"],
        cookies=login_details["cookies"],
    )
    unload_dir = vars.get('DOWNLOAD_DIR')
    logging.info("Found %s worksheets", len(worksheets))
    # loop over the returned worksheets

    for worksheet in worksheets.values():
        file_extension = 'sql' if worksheet['query_language'] == 'sql' else 'py'

        # Handle potential None value for folder_name
        folder_path = os.path.join(unload_dir, worksheet['folder_name']) if worksheet['folder_name'] else unload_dir

        # Create folders if necessary (this won't create any folders for None folder_name)
        os.makedirs(folder_path, exist_ok=True)
        # Sanitize the worksheet name for use as a file name
        sanitized_worksheet_name = re.sub(r'[^\w.-]', '-', worksheet['worksheet_name'])
        file_name = f"{sanitized_worksheet_name}.{file_extension}"
        unload_path = os.path.join(folder_path, file_name)

        query = worksheet['query_value']

        logging.info(f'''Name: {worksheet["worksheet_name"]} and URL:  {worksheet["worksheet_url"]}''')
        # # Open a file for writing in SQL format
        with open(unload_path, "w",encoding='utf-8') as sql_file:
            sql_file.write(query)
        logging.info(f"Query successfully written to {unload_path}")
        