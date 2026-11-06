-- Implements: FA98 (osm2pgsql-flex-Stil des Serverpakets)
--
-- Nachbau des Stils /styles/pipeline.lua der NAS-Instanz aus dem dort introspektierten
-- Schema; das Original liegt nicht im Repository. Maßgeblich ist das
-- Schema, gegen das src/geofact/builtin/sources/osm.py (PostgisBackend) abfragt:
--
--   osm_points    node_id bigint, tags jsonb, geom geometry(Point, 4326)
--   osm_lines     way_id bigint,  tags jsonb, geom geometry(LineString, 4326)
--   osm_polygons  osm_type char(1), osm_id bigint, tags jsonb, area_m2 real,
--                 geom geometry(MultiPolygon, 4326)
--
-- je mit GiST-Index auf geom und GIN-Index auf tags. Das Schema der Tabellen setzt
-- import.sh über --schema (Staging-Schema osm_import, danach Tausch gegen osm).
--
-- Abweichen kann vom Original nur die Regel, welche geschlossenen Wege Flächen sind
-- (is_area unten). Wer das Original hat, ersetzt diese Datei damit.

local SRID = 4326

local function indexes()
    return {
        { column = 'geom', method = 'gist', with = 'fillfactor=100' },
        { column = 'tags', method = 'gin' },
    }
end

local points = osm2pgsql.define_node_table('osm_points', {
    { column = 'tags', type = 'jsonb' },
    { column = 'geom', type = 'point', projection = SRID, not_null = true },
}, { indexes = indexes() })

local lines = osm2pgsql.define_way_table('osm_lines', {
    { column = 'tags', type = 'jsonb' },
    { column = 'geom', type = 'linestring', projection = SRID, not_null = true },
}, { indexes = indexes() })

local polygons = osm2pgsql.define_table({
    name = 'osm_polygons',
    -- 'any' statt 'area': nur dann legt osm2pgsql die Spalte osm_type (W/R) an; Knoten
    -- landen hier nie, process_node schreibt nur in osm_points.
    ids = { type = 'any', id_column = 'osm_id', type_column = 'osm_type' },
    columns = {
        { column = 'tags', type = 'jsonb' },
        { column = 'area_m2', type = 'real' },
        { column = 'geom', type = 'multipolygon', projection = SRID, not_null = true },
    },
    indexes = indexes(),
})

-- Tags ohne Aussage für Analysen (Editor-Metadaten).
local DROP_KEYS = { created_by = true, ['odbl'] = true, ['odbl:note'] = true }

local function clean(tags)
    local out, any = {}, false
    for k, v in pairs(tags) do
        if not DROP_KEYS[k] then
            out[k] = v
            any = true
        end
    end
    return any and out or nil
end

-- Schlüssel, deren geschlossene Wege Flächen sind (Werte, die es nicht sind, in EXCEPT).
local AREA_KEYS = {
    building = true, ['building:part'] = true, landuse = true, leisure = true,
    amenity = true, shop = true, office = true, craft = true, tourism = true,
    historic = true, military = true, healthcare = true, emergency = true,
    place = true, water = true, wetland = true, golf = true, boundary = true,
    natural = true, man_made = true, aeroway = true, power = true, railway = true,
    public_transport = true, waterway = true, highway = true,
}

local EXCEPT = {
    natural = { coastline = true, cliff = true, ridge = true, arete = true, tree_row = true,
                earth_bank = true, gully = true },
    man_made = { embankment = true, breakwater = true, groyne = true, dyke = true,
                 cutline = true, pipeline = true },
    aeroway = { taxiway = true, runway = true, parking_position = true },
    boundary = { administrative = true, political = true },
}

-- Nur diese Werte machen einen geschlossenen Weg zur Fläche.
local ONLY = {
    power = { plant = true, substation = true, generator = true, transformer = true,
              switchgear = true, converter = true },
    railway = { platform = true, station = true, turntable = true },
    public_transport = { platform = true, station = true },
    waterway = { riverbank = true, dock = true, boatyard = true, dam = true },
    highway = { pedestrian = true, services = true, rest_area = true, platform = true },
}

local function is_area(tags)
    if tags.area == 'yes' then return true end
    if tags.area == 'no' then return false end
    for k, v in pairs(tags) do
        if AREA_KEYS[k] then
            local only = ONLY[k]
            local except = EXCEPT[k]
            if only then
                if only[v] then return true end
            elseif not (except and except[v]) then
                return true
            end
        end
    end
    return false
end

local function insert_polygon(object, tags, geom)
    if geom:is_null() then return end
    polygons:insert({ tags = tags, area_m2 = geom:spherical_area(), geom = geom })
end

function osm2pgsql.process_node(object)
    local tags = clean(object.tags)
    if tags then
        points:insert({ tags = tags, geom = object:as_point() })
    end
end

function osm2pgsql.process_way(object)
    local tags = clean(object.tags)
    if not tags then return end
    if object.is_closed and is_area(tags) then
        insert_polygon(object, tags, object:as_polygon())
    else
        local geom = object:as_linestring()
        if not geom:is_null() then
            lines:insert({ tags = tags, geom = geom })
        end
    end
end

function osm2pgsql.process_relation(object)
    local kind = object.tags.type
    if kind ~= 'multipolygon' and kind ~= 'boundary' then return end
    local tags = clean(object.tags)
    if tags then
        insert_polygon(object, tags, object:as_multipolygon())
    end
end
