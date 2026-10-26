-- Upgrade the historical 1.1.0 token layout (nz=1 ... rz=55) to 1.1.1.
-- Replace the library and run UPDATE before changing parser mappings. Changes
-- made with the new library while SQL is still 1.1.0 cannot be distinguished
-- from historical numeric mappings and are outside this migration contract.
CREATE FUNCTION jieba_mp_start(internal, integer)
RETURNS internal AS 'MODULE_PATHNAME' LANGUAGE C STRICT;

CREATE FUNCTION jieba_hmm_start(internal, integer)
RETURNS internal AS 'MODULE_PATHNAME' LANGUAGE C STRICT;

CREATE FUNCTION jieba_reload_dict()
RETURNS void AS 'MODULE_PATHNAME' LANGUAGE C STRICT;

CREATE TEXT SEARCH PARSER jiebamp (
    START = jieba_mp_start, GETTOKEN = jieba_gettoken, END = jieba_end,
    LEXTYPES = jieba_lextype, HEADLINE = pg_catalog.prsd_headline
);
CREATE TEXT SEARCH PARSER jiebahmm (
    START = jieba_hmm_start, GETTOKEN = jieba_gettoken, END = jieba_end,
    LEXTYPES = jieba_lextype, HEADLINE = pg_catalog.prsd_headline
);

CREATE TEXT SEARCH CONFIGURATION jiebamp (PARSER = jiebamp);
COMMENT ON TEXT SEARCH CONFIGURATION jiebamp IS 'MP segmentation configuration for jieba';
CREATE TEXT SEARCH CONFIGURATION jiebahmm (PARSER = jiebahmm);
COMMENT ON TEXT SEARCH CONFIGURATION jiebahmm IS 'Hmm segmentation configuration for jieba';

ALTER TEXT SEARCH CONFIGURATION jiebamp ADD MAPPING FOR eng,nz,n,m,i,l,d,s,t,mq,nr,j,a,r,b,f,nrt,v,z,ns,q,vn,c,nt,u,o,zg,nrfg,df,p,g,y,ad,vg,ng,x,ul,k,ag,dg,rr,rg,an,vq,e,uv,tg,mg,ud,vi,vd,uj,uz,h,ug,rz WITH jieba_stem;
ALTER TEXT SEARCH CONFIGURATION jiebahmm ADD MAPPING FOR eng,nz,n,m,i,l,d,s,t,mq,nr,j,a,r,b,f,nrt,v,z,ns,q,vn,c,nt,u,o,zg,nrfg,df,p,g,y,ad,vg,ng,x,ul,k,ag,dg,rr,rg,an,vq,e,uv,tg,mg,ud,vi,vd,uj,uz,h,ug,rz WITH jieba_stem;

DO $upgrade$
DECLARE
    old_aliases constant text[] := ARRAY[
        'nz','n','m','i','l','d','s','t','mq','nr','j','a','r','b','f','nrt',
        'v','z','ns','q','vn','c','nt','u','o','zg','nrfg','df','p','g','y',
        'ad','vg','ng','x','ul','k','ag','dg','rr','rg','an','vq','e','uv',
        'tg','mg','ud','vi','vd','uj','uz','h','ug','rz'
    ];
    extension_oid oid;
    extension_namespace oid;
    member_extension oid;
    configuration record;
    current_alias text;
    saved_mappings jsonb;
    mapping jsonb;
    dictionaries text;
    config_name text;
    config_oid oid;
    english_token integer;
BEGIN
    SELECT oid, extnamespace INTO STRICT extension_oid, extension_namespace
      FROM pg_catalog.pg_extension WHERE extname = 'pg_jieba';

    -- A user configuration may be in any schema and may use several ordered
    -- dictionaries. Snapshot and remove all old rows before adding any new ID.
    FOR configuration IN
        SELECT config.oid, config.cfgparser
          FROM pg_catalog.pg_ts_config AS config
          JOIN pg_catalog.pg_ts_parser AS parser ON parser.oid = config.cfgparser
         WHERE parser.prsnamespace = extension_namespace
           AND parser.prsname IN ('jieba', 'jiebaqry')
    LOOP
        SELECT dependency.refobjid INTO member_extension
          FROM pg_catalog.pg_depend AS dependency
         WHERE dependency.classid = 'pg_catalog.pg_ts_config'::regclass
           AND dependency.objid = configuration.oid AND dependency.objsubid = 0
           AND dependency.refclassid = 'pg_catalog.pg_extension'::regclass
           AND dependency.deptype = 'e';
        IF member_extension IS NOT NULL AND member_extension <> extension_oid THEN
            RAISE EXCEPTION 'pg_jieba cannot migrate configuration % owned by another extension',
                configuration.oid::regconfig;
        END IF;

        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_ts_config_map
             WHERE mapcfg = configuration.oid
               AND maptokentype NOT BETWEEN 1 AND pg_catalog.array_length(old_aliases, 1)
        ) THEN
            RAISE EXCEPTION 'pg_jieba 1.1.0 configuration % does not use the historical token layout',
                configuration.oid::regconfig;
        END IF;

        SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                   'alias', old_aliases[old_mapping_rows.maptokentype], 'dictionaries', old_mapping_rows.dictionaries))
          INTO saved_mappings
          FROM (
              SELECT maptokentype, pg_catalog.array_agg(mapdict ORDER BY mapseqno) AS dictionaries
                FROM pg_catalog.pg_ts_config_map WHERE mapcfg = configuration.oid
               GROUP BY maptokentype
          ) AS old_mapping_rows;

        FOR mapping IN SELECT value FROM pg_catalog.jsonb_array_elements(saved_mappings)
        LOOP
            PERFORM 1 FROM pg_catalog.ts_token_type(configuration.cfgparser)
             WHERE alias = mapping->>'alias';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'pg_jieba new parser lacks historical token alias %', mapping->>'alias';
            END IF;
        END LOOP;

        -- PostgreSQL permits mapping DDL during UPDATE only for members of
        -- the updating extension. Attach free-standing configurations only
        -- for this atomic migration, then restore their original membership.
        IF member_extension IS NULL THEN
            EXECUTE pg_catalog.format('ALTER EXTENSION pg_jieba ADD TEXT SEARCH CONFIGURATION %s',
                configuration.oid::regconfig);
        END IF;

        FOR mapping IN SELECT value FROM pg_catalog.jsonb_array_elements(saved_mappings)
        LOOP
            SELECT types.alias INTO STRICT current_alias
              FROM pg_catalog.ts_token_type(configuration.cfgparser) AS types
             WHERE types.tokid = pg_catalog.array_position(old_aliases, mapping->>'alias');
            EXECUTE pg_catalog.format('ALTER TEXT SEARCH CONFIGURATION %s DROP MAPPING FOR %I',
                configuration.oid::regconfig, current_alias);
        END LOOP;

        FOR mapping IN SELECT value FROM pg_catalog.jsonb_array_elements(saved_mappings)
        LOOP
            SELECT pg_catalog.string_agg(pg_catalog.format('%I.%I', namespace.nspname, dictionary.dictname),
                       ', ' ORDER BY ids.position)
              INTO dictionaries
              FROM pg_catalog.jsonb_array_elements_text(mapping->'dictionaries')
                   WITH ORDINALITY AS ids(dictionary_oid, position)
              JOIN pg_catalog.pg_ts_dict AS dictionary ON dictionary.oid = ids.dictionary_oid::oid
              JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = dictionary.dictnamespace;
            EXECUTE pg_catalog.format('ALTER TEXT SEARCH CONFIGURATION %s ADD MAPPING FOR %I WITH %s',
                configuration.oid::regconfig, mapping->>'alias', dictionaries);
        END LOOP;

        IF member_extension IS NULL THEN
            EXECUTE pg_catalog.format('ALTER EXTENSION pg_jieba DROP TEXT SEARCH CONFIGURATION %s',
                configuration.oid::regconfig);
        END IF;
    END LOOP;

    -- Only the extension's two standard configurations gain the new English
    -- default; custom configurations keep their historical token coverage.
    FOREACH config_name IN ARRAY ARRAY['jiebacfg', 'jiebaqry'] LOOP
        config_oid := config_name::regconfig::oid;
        SELECT token.tokid INTO STRICT english_token
          FROM pg_catalog.pg_ts_config AS config,
               pg_catalog.ts_token_type(config.cfgparser) AS token
         WHERE config.oid = config_oid AND token.alias = 'eng';
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_ts_config_map
             WHERE mapcfg = config_oid AND maptokentype = english_token
        ) THEN
            EXECUTE pg_catalog.format(
                'ALTER TEXT SEARCH CONFIGURATION %s ADD MAPPING FOR eng WITH jieba_stem',
                config_oid::regconfig
            );
        END IF;
    END LOOP;
END;
$upgrade$;
