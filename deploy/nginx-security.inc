# MAGI 每个 server 块共用的安全策略（install 到 snippets/magi-security.conf）。
#
# 只放行正常方法、挡掉扫描器爱敲的路径、补齐安全响应头。
# 页面里用了内联 <script>/<style>，所以 CSP 保留 'unsafe-inline'；
# 它挡不住内联注入，但能挡住外链脚本、object/embed 和被别的站点嵌套。

add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
add_header Cross-Origin-Opener-Policy "same-origin" always;
add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'" always;

# 只允许这几种方法；TRACE、TRACK 和扫描器常用的怪方法直接 405。
if ($request_method !~ ^(GET|HEAD|POST|PUT|DELETE|OPTIONS)$) {
    return 405;
}

# 点开头的路径（.git/.env/.svn…）一律 404，只放行 ACME 校验用的 .well-known。
location ~* /\.(?!well-known/) {
    return 404;
}

# 静态目录里不会出现这些后缀；扫描器敲过来就别再往后端送了。
location ~* \.(env|git|svn|hg|bak|old|orig|sql|sqlite|db|ini|cfg|conf|log|sh|py|yml|yaml|pem|key)$ {
    return 404;
}
