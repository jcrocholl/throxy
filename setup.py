from distutils.core import setup

setup(
    name='throxy.py',
    version='0.1',
    description='Throttling HTTP proxy in one Python file',
    long_description="""\
* Simulate a slow connection (like dial-up).
* Adjustable bandwidth limit for download and upload.
* Optionally dump HTTP headers and content for debugging.
* Decompress gzip content encoding for debugging.
* Multiple connections, without threads (uses asyncore).
* Only one source file, written in pure Python.
""",
    license='http://www.opensource.org/licenses/mit-license.php',
    author='Johann C. Rocholl',
    author_email='johann@browsershots.org',
    url='http://svn.browsershots.org/trunk/throxy/throxy.py',
    scripts=['throxy.py'],
    )
