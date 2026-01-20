"""
common.py

Shared constants and utilities for House Planner CDK stacks.
"""


def get_warmup_page_html() -> str:
    """
    Returns the HTML for the warm-up/starting page.
    
    This page is shown by ALB default action after OIDC auth.
    It triggers the /internal/ensure endpoint, waits for response (sets routing cookie),
    then polls /health every 3s until the instance is ready, then refreshes.
    
    Note: ALB fixed response has a 1024 byte limit, so keep this compact.
    """
    # Compact JS: ensure→cookie stored, then poll /health until ready, then reload
    # This avoids the 502 during boot by waiting for EC2 to be healthy
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
        "<p id=s>Starting your workspace...</p>"
        "</div>"
        "<script>"
        "var n=0,ok=0;"
        "fetch('/internal/ensure',{credentials:'include'})"
        ".then(function(r){if(r.ok)ok=1;return r.text()})"
        ".then(function(t){poll(ok?'ok':'err:'+t.substring(0,15))})"
        ".catch(function(e){poll('net:'+e)});"
        "function poll(s){"
        "n++;var m='Starting... ('+n*3+'s) '+s;"
        "fetch('/health',{credentials:'include'})"
        ".then(function(r){return r.text()})"
        ".then(function(t){var x=t.trim();if(x=='OK')location.reload();else{document.getElementById('s').textContent=m+' ['+x.substring(0,15)+']';setTimeout(function(){poll(s)},3000)}})"
        ".catch(function(e){document.getElementById('s').textContent=m+' err:'+e;setTimeout(function(){poll(s)},3000)})}"
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
        "<p>Starting your workspace...</p>"
        "<p>This page will refresh automatically.</p>"
        "</div>"
        "</body>"
        "</html>"
    )
