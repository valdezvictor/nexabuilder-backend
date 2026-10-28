import re as _re_helper

def extract_internal_links(html):
    # Use chr(34)=double-quote chr(39)=single-quote to avoid string delimiter conflicts
    dq = chr(34)
    sq = chr(39)
    pat = r'<a[^>]+href=[' + dq + sq + r']([^' + dq + sq + r']+)[' + dq + sq + r'][^>]*>([^<]+)</a>'
    found = [(u.strip(), t.strip()) for u, t in _re_helper.findall(pat, html, _re_helper.I)
             if u.strip().startswith('/') and not u.strip().startswith('//')]
    return found

def build_link_section(found_links):
    if not found_links:
        return ''
    lines = ['- [' + t + '](' + u + ')' for u, t in found_links]
    return chr(10) + chr(10) + '## INTERNAL LINKS IN ARTICLE' + chr(10) + chr(10).join(lines)
