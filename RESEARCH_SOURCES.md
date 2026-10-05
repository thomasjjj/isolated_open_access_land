# Access evidence and the Dorset pilot

The regional workflow includes CRoW open country, registered common land, section 16 dedication and the Natural England section 15 inventory. These describe access rights, not public ownership. Section 15 records preserve the common name and the Act under which the source records access; they need current local interpretation. Overlapping land contributes its area once. The source catalogue is [Natural England's section 15 land dataset](https://environment.data.gov.uk/dataset/72eda560-eee8-4667-a588-b5289c4ae011).

OSM `designation=common`, `leisure=common` and `landuse=common` polygons remain unverified clues. A common name or land-use tag does not establish a registered common or statutory access. They are excluded from the official site inventory and from positive public/permissive classifications. Their raw access tags, geometry and source IDs are retained for local review.

The implemented permissive category requires a strong connection in the public-plus-permissive graph at 0 m, the configured tolerance and at least 5 m. Consistent public access takes precedence. Unknown routes, weak contacts and unverified areas can support uncertainty. A permissive finding does not exclude an unmapped public approach, and permission can change.

## Ordnance Survey sources

OS Open Greenspace supplies generalised greenspace polygons and entrances. OS explicitly says that inclusion of either a site or an entrance does not guarantee public access. It is suitable as a future contextual layer, requiring validation before it affects connectivity. It is deferred in this implementation. See the [product details](https://docs.os.uk/os-downloads/products/land-and-terrain-portfolio/os-open-greenspace/os-open-greenspace-overview/product-details) and [data-creation guidance](https://docs.os.uk/os-downloads/products/land-and-terrain-portfolio/os-open-greenspace/os-open-greenspace-overview/data-creation).

OS Detailed Path Network documents a `rightOfUse` attribute that distinguishes designated rights, including permissive paths. It is a separate product from OS Open Greenspace and OS OpenMap Local; this project does not acquire it or presume licensing eligibility. The current implementation uses council-derived PRoW and explicitly tagged OSM permissions. See the [OS RouteLink specification](https://docs.os.uk/os-downloads/products/transport-network-portfolio/os-detailed-path-network/os-detailed-path-network-technical-specification/feature-types/routelink).

## Dorset

The pilot selects Dorset Council, E06000059. Bournemouth, Christchurch and Poole is a separate authority and is outside the study boundary, although neighbouring routes and complete cross-border sites can appear as context.

Dorset has a manageable existing land inventory and has discussed access-island work in its Local Access Forum. This is a prioritisation reason, not evidence of the number of inaccessible sites. See the [2022 forum minutes](https://www.dorsetcouncil.gov.uk/w/dlaf-minutes-31-march-2022) and [28 April 2026 minutes](https://www.dorsetcouncil.gov.uk/documents/d/guest/dlaf-minutes-from-transcript-28-april-2026).

On 5 October 2026 the council's linked OGL route download was acquired separately for desk comparison: 6,940 route features, all labelled Definitive in the export. Its operational field records 6,859 Open, 46 Part of Path Closed and 35 Closed. Those are source-record attributes, not field verification. The approximate published lines cannot replace the legal definitive map. The analysis retains its recorded NE/OSM source snapshots; the fresh council download supports dated desk checks and identifies questions for a future source update. Sources: [Dorset's route map and download](https://www.dorsetcouncil.gov.uk/w/rights-of-way-map-where-to-walk-ride-or-cycle) and [definitive-map guidance](https://www.dorsetcouncil.gov.uk/w/definitive-map-and-statement).

The dated desk-review ledger links each checked site to the council download, its route identifiers, and the statutory or OSM evidence inspected. It records unresolved entrances, permission and physical access explicitly. No council contact or field inspection is implied.
