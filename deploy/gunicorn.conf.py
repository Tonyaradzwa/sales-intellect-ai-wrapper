"""gunicorn settings for the Flask backend (loaded via --config in
sales-intellect-flask.service). Kept in a file rather than on the ExecStart
line so the access-log format doesn't need systemd's %-escaping.

The access log goes to stdout, i.e. the service's journal:
    journalctl -u sales-intellect-flask --since "..." --until "..."
`attempt=` is the X-Request-ID the Streamlit app sends, the same attempt_id
that's on every line of events.log.
"""

accesslog = "-"
access_log_format = '%(t)s "%(r)s" %(s)s %(b)s %(D)sus attempt=%({x-request-id}i)s'
