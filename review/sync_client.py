"""Explicit one-shot trigger; a lost response is unconfirmed, never auto retried."""
import json
import os
import urllib.request
from gate_reader.client import BASE_URL, _NoRedirect
from .verified_sync import request_id

class SyncUnconfirmed(Exception): pass

def trigger(identifier, timeout=180):
    request_id(identifier)
    token=os.environ.get('REVIEW_SYNC_TOKEN','')
    if not token or any(ord(c)<32 or ord(c)>126 for c in token):
        raise ValueError('Configure protected sync credential')
    body=json.dumps({'request_id':identifier}).encode()
    req=urllib.request.Request(BASE_URL+'/review-sync',data=body,method='POST',
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler(),_NoRedirect())
        with opener.open(req,timeout=timeout) as response:
            if response.geturl()!=BASE_URL+'/review-sync' or response.status!=200:raise ValueError()
            data=json.loads(response.read(2049))
        marker=data['last_verified_sync']
        if data.get('ok') is not True or marker.get('request_id')!=identifier or marker.get('matches_current_ledger') is not True:
            raise ValueError()
        return marker
    except Exception:
        raise SyncUnconfirmed('Sync unconfirmed; do not retrigger; verify matching marker in review-summary') from None


def verified_in_summary(identifier, summary):
    request_id(identifier)
    m=summary.get('data',{}).get('last_verified_sync')
    if type(summary.get('data',{}).get('as_of')) is not int:
        return False
    return bool(m and m.get('request_id')==identifier and m.get('matches_current_ledger') is True and
                type(m.get('cutoff')) is int and summary['data'].get('as_of',-1)>=m['cutoff'])
