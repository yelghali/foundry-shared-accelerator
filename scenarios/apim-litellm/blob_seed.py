"""Seed the single verification blob using a dedicated writer identity."""

import os


def main():
    from azure.identity import WorkloadIdentityCredential
    from azure.storage.blob import BlobServiceClient

    with WorkloadIdentityCredential() as credential:
        with BlobServiceClient(
            os.environ["BLOB_ACCOUNT_URL"], credential=credential
        ) as service:
            service.get_blob_client(
                container=os.environ["BLOB_CONTAINER"], blob="gateway-check.txt"
            ).upload_blob(b"MCP_WORKLOAD_IDENTITY_OK", overwrite=True)


if __name__ == "__main__":
    main()
