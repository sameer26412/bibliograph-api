import hashlib
from typing import Any
import xml.etree.ElementTree as ET

from app.schemas import ResourceOut

OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NS = "http://purl.org/dc/elements/1.1/"

ET.register_namespace("oai_dc", OAI_DC_NS)
ET.register_namespace("dc", DC_NS)


def build_etag(payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f'"{digest}"'


def resolve_response_format(format_param: str | None, accept_header: str | None) -> str:
    if format_param:
        return format_param.lower()

    accept = (accept_header or "").lower()
    if "application/ld+json" in accept:
        return "jsonld"
    if "application/xml" in accept or "text/xml" in accept:
        return "xml"
    return "json"


def serialize_resource_jsonld(resource: ResourceOut, canonical_url: str) -> dict[str, Any]:
    availability = (
        "https://schema.org/InStock"
        if resource.available_copies > 0
        else "https://schema.org/OutOfStock"
    )
    payload: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Book",
        "@id": canonical_url,
        "isbn": resource.isbn,
        "name": resource.title,
        "description": resource.summary,
        "datePublished": str(resource.publication_year),
        "inLanguage": resource.language,
        "publisher": {
            "@type": "Organization",
            "name": resource.publisher,
        },
        "author": [{"@type": "Person", "name": name} for name in resource.authors],
        "keywords": ", ".join(resource.tags),
        "offers": {
            "@type": "Offer",
            "availability": availability,
            "inventoryLevel": {
                "@type": "QuantitativeValue",
                "value": resource.available_copies,
            },
        },
    }
    if resource.source_url:
        payload["sameAs"] = resource.source_url
    return payload


def serialize_resource_dublin_core_xml(resource: ResourceOut, canonical_url: str) -> bytes:
    root = ET.Element(f"{{{OAI_DC_NS}}}dc")

    def add_dc(tag: str, text: str | None) -> None:
        if text:
            elem = ET.SubElement(root, f"{{{DC_NS}}}{tag}")
            elem.text = text

    add_dc("title", resource.title)
    for author in resource.authors:
        add_dc("creator", author)
    for tag in resource.tags:
        add_dc("subject", tag)
    add_dc("description", resource.summary)
    add_dc("publisher", resource.publisher)
    add_dc("date", str(resource.publication_year))
    add_dc("type", "Text")
    add_dc("format", "application/xml")
    add_dc("identifier", f"urn:isbn:{resource.isbn}")
    add_dc("identifier", canonical_url)
    if resource.source_url:
        add_dc("relation", resource.source_url)
    add_dc("language", resource.language)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)