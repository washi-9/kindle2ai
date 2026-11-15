#!/bin/sh
# Create a local, self-signed code-signing identity for Kindle2AI.app.
#
# Why: an ad-hoc signed app is identified by the hash of its exact build, so
# every rebuild silently invalidates the Accessibility and Screen Recording
# permissions granted to it -- System Settings still shows the switch on, but
# macOS refuses. Signed with a fixed certificate, the app is identified by
# that certificate instead and keeps its permissions across rebuilds.
#
# The certificate lives only in your login keychain, is valid only for code
# signing, and is used by ./setup.sh. Remove it any time in Keychain Access
# ("Kindle2AI Local Signing").
set -e
NAME="Kindle2AI Local Signing"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -p codesigning "$KEYCHAIN" 2>/dev/null | grep -q "$NAME"; then
    echo "\"$NAME\" already exists."
    exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF

# /usr/bin/openssl (LibreSSL) writes a PKCS#12 that `security` can import;
# Homebrew's OpenSSL 3 would need -legacy.
/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -config "$TMP/cert.cnf" 2>/dev/null
/usr/bin/openssl pkcs12 -export -out "$TMP/id.p12" \
    -inkey "$TMP/key.pem" -in "$TMP/cert.pem" -passout pass:kindle2ai
security import "$TMP/id.p12" -k "$KEYCHAIN" -P kindle2ai -T /usr/bin/codesign

# Check that codesign accepts it; if the self-signed certificate is not
# trusted for code signing, trust it (macOS asks for your password).
probe="$TMP/probe"
cp /usr/bin/true "$probe"
if ! codesign --force --sign "$NAME" "$probe" 2>/dev/null; then
    echo "trusting the certificate for code signing (macOS may ask for your password)"
    security add-trusted-cert -p codeSign -k "$KEYCHAIN" "$TMP/cert.pem"
    codesign --force --sign "$NAME" "$probe"
fi
echo "created \"$NAME\". Now run ./setup.sh to re-sign Kindle2AI.app."
