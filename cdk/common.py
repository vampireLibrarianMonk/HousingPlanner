"""
common.py

Shared constants and utilities for House Planner CDK stacks.
"""


def get_warmup_page_html() -> str:
    """
    Returns the HTML for the warm-up/starting page.
    
    This page is shown by ALB default action after OIDC auth.
    It triggers the /internal/ensure endpoint, waits for response (sets routing cookie),
    then polls /health until the instance is ready, then refreshes.
    
    Note: ALB fixed response has a 1024 byte limit, so keep this compact.
    
    Steps displayed:
    1. Starting Lambda - calling /internal/ensure
    2. Creating workspace - Lambda creating/starting EC2
    3. Booting instance - waiting for health check
    4. Ready - redirecting to app
    """
    return (
        "<!DOCTYPE html>"
        "<html>"
        "<head>"
        '<meta charset="UTF-8">'
        "<title>House Planner</title>"
        "<style>"
        "body{font-family:sans-serif;display:flex;justify-content:center;"
        "align-items:center;height:100vh;margin:0;background:#667eea;color:#fff;text-align:center}"
        "</style>"
        "</head>"
        "<body>"
        "<div>"
        "<h1>House Planner</h1>"
        "<p id=s>Step 1/4: Starting Lambda</p>"
        "</div>"
        "<script>"
        "var S=document.getElementById('s');"
        "fetch('/internal/ensure',{credentials:'include'})"
        ".then(r=>{if(!r.ok)throw'Error:'+r.status;return new Promise(x=>setTimeout(()=>x(r),1000))})"
        ".then(r=>{S.textContent='Step 2/4: Creating workspace';return r.text()})"
        ".then(()=>new Promise(x=>setTimeout(x,1000)))"
        ".then(()=>{S.textContent='Step 3/4: Booting instance';poll()})"
        ".catch(e=>{S.textContent='FAILED - '+e});"
        "function poll(){"
        "fetch('/health',{credentials:'include'}).then(r=>r.text())"
        ".then(t=>{if(t.trim()=='OK'){S.textContent='Step 4/4: Ready!';setTimeout(()=>location.reload(),1000)}else setTimeout(poll,30000)})"
        ".catch(()=>setTimeout(poll,30000))}"
        "</script>"
        "</body>"
        "</html>"
    )


def get_nginx_warmup_page_html() -> str:
    """
    Returns the HTML for nginx's warm-up page (starting.html).
    
    This is similar to get_warmup_page_html() but without the JavaScript
    that calls /internal/ensure (nginx pages don't need to trigger provisioning).
    """
    return (
        "<!DOCTYPE html>"
        "<html>"
        "<head>"
        '<meta charset="UTF-8">'
        "<title>House Planner</title>"
        '<meta http-equiv="refresh" content="5">'
        "<style>"
        "body{font-family:sans-serif;display:flex;justify-content:center;"
        "align-items:center;height:100vh;margin:0;background:#667eea;color:#fff;text-align:center}"
        "</style>"
        "</head>"
        "<body>"
        "<div>"
        "<h1>House Planner</h1>"
        "<p>Starting your streamlit application...</p>"
        "<p>This page will refresh automatically.</p>"
        "</div>"
        "</body>"
        "</html>"
    )
