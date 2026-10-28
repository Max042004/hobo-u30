# Third-party software

The service itself uses Python's standard library. Excel import/export bundles:

| Package | Version | License | Source |
|---|---|---|---|
| xlrd | 2.0.2 | BSD | https://github.com/python-excel/xlrd |
| xlwt | 1.3.0 | BSD / upstream component notices | https://github.com/python-excel/xlwt |

Full notices are retained in `vendor/xlrd-2.0.2.dist-info/LICENSE` and
`vendor/xlwt/LICENSE`. This product includes software developed by
Roman V. Kiseliov <roman@kiseliov.ru>.

Raspberry Pi OS / Debian packages and optional Cloudflare `cloudflared` are
installed from their upstream repositories and remain subject to their own licenses.
HOBOware binaries, decompiled source, device credentials and full logger backups
are not included in this repository. This project is an independent implementation.
