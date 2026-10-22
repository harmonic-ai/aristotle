Certificates for `api_request_test.py`'s TLS tests, valid until 2126. The two CAs differ only in the keyUsage
extension, which Python 3.13's default context requires of a CA; each signed a certificate for localhost and
127.0.0.1. Made with OpenSSL 3, for each `$kind` of `with-keyusage` and `without-keyusage`:

```
cat > ca.cnf <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = Aristotle test CA $kind
[ca]
basicConstraints = critical, CA:TRUE
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
keyUsage = critical, keyCertSign, cRLSign    # without-keyusage leaves this line out
EOF
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca-$kind.pem -days 36500 -config ca.cnf -extensions ca
openssl req -newkey rsa:2048 -nodes -keyout server-of-ca-$kind.key -out server.csr -subj "/CN=localhost"
cat > server.ext <<EOF
subjectAltName = DNS:localhost, IP:127.0.0.1
extendedKeyUsage = serverAuth
keyUsage = critical, digitalSignature, keyEncipherment
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
basicConstraints = critical, CA:FALSE
EOF
openssl x509 -req -in server.csr -CA ca-$kind.pem -CAkey ca.key -CAcreateserial -days 36500 -extfile server.ext \
    -out server-of-ca-$kind.pem
```

The CA keys were thrown away.
