import warnings

# macOS system Python (LibreSSL) triggers urllib3 NotOpenSSLWarning; suppress to keep UI logs clean.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+.*")

from takealot_autolister.cli import main

if __name__ == "__main__":
    main()
