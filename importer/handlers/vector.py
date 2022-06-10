from django.conf import settings
from importer.handlers.base import GEOM_TYPE_MAPPING, STANDARD_TYPE_MAPPING, AbstractHandler
from dynamic_models.models import ModelSchema, FieldSchema
import os
from subprocess import Popen, PIPE
from osgeo import ogr
from geonode.resource.models import ExecutionRequest
from django.utils import timezone


class GPKGFileHandler(AbstractHandler):
    '''
    Handler to import GPK files into GeoNode data db
    It must provide the task_lists required to comple the upload
    '''
    TASKS_LIST = (
        "start_import",
        "importer.import_resource",
        "importer.publish_resource",
        "importer.create_gn_resource",
    )

    def is_valid(self, files):
        """
        Define basic validation steps
        """        
        return all([os.path.exists(x) for x in files.values()])

    def import_resource(self, files: dict, execution_id: str) -> str:
        '''
        Main function to import the resource.
        Internally will cal the steps required to import the 
        data inside the geonode_data database
        '''
        layers = ogr.Open(files.get("base_file"))
        # for the moment we skip the dyanamic model creation

        for layer in layers:
            self._update_execution_request(
                execution_id=execution_id,
                last_updated=timezone.now(),
                log=f"setting up dynamic model for layer: {layer.GetName()}"
            )
            self._setup_dynamic_model(layer)

        self._update_execution_request(
            execution_id=execution_id,
            last_updated=timezone.now(),
            log=f"importing layer"
        )
        stdout = self._run_ogr2ogr_import(files)
        return stdout

    def _setup_dynamic_model(self, layer):
        '''
        Extract from the geopackage the layers name and their schema
        after the extraction define the dynamic model instances
        '''
        # TODO: finish the creation, is raising issues due the NONE value of the table
        foi_schema, created = ModelSchema.objects.get_or_create(
            name=layer.GetName(),
            db_name="datastore",
            is_managed=False,
            use_applable_as_table_prefix=False
        )
        if not created:
            return foi_schema.as_model()
        # define standard field mapping from ogr to django
        dynamic_model = self.create_dynamic_model_instance(layer=layer, dynamic_model_schema=foi_schema)
        return dynamic_model
    
    def create_dynamic_model_instance(self, layer, dynamic_model_schema):
        layer_schema = [
            {"name": x.name.lower(), "class_name": self._get_type(x), "null": True}
            for x in layer.schema
        ]
        layer_schema += [
            {
                "name": layer.GetGeometryColumn(),
                "class_name": GEOM_TYPE_MAPPING.get(ogr.GeometryTypeToName(layer.GetGeomType()))
            }
        ]

        for field in layer_schema:
            _kwargs = {"null": field.get('null', True)}
            if field['class_name'].endswith('CharField'):
                _kwargs = {**_kwargs, **{"max_length": 255}}
            FieldSchema.objects.create(
                name=field['name'],
                class_name=field['class_name'],
                model_schema=dynamic_model_schema,
                kwargs=_kwargs
            )

        return dynamic_model_schema.as_model()

    def _update_execution_request(self, execution_id, **kwargs):
        ExecutionRequest.objects.filter(exec_id=execution_id).update(
            status=ExecutionRequest.STATUS_RUNNING, **kwargs
        )

    def _run_ogr2ogr_import(self, files):
        '''
        Perform the ogr2ogr command to import he gpkg inside geonode_data
        '''
        ogr_exe = "/usr/bin/ogr2ogr"
        _uri = settings.GEODATABASE_URL.replace("postgis://", "")
        db_user, db_password = _uri.split('@')[0].split(":")
        db_host, db_port = _uri.split('@')[1].split('/')[0].split(":")
        db_name = _uri.split('@')[1].split("/")[1]
        
        options = '-progress '
        options += '--config PG_USE_COPY YES '
        options += '-f PostgreSQL PG:" dbname=\'%s\' host=%s port=%s user=\'%s\' password=\'%s\' " ' \
                   % (db_name, db_host, db_port, db_user, db_password)
        options += files.get("base_file") + " "
        options += '-lco DIM=2 '
        options += '-overwrite '

        commands = [ogr_exe] + options.split(" ")
        
        process = Popen(' '.join(commands), stdout=PIPE, stderr=PIPE, shell=True)
        stdout, stderr = process.communicate()
        if stderr is not None and stderr != b'':
            raise Exception(stderr)
        return stdout

    def _get_type(self, _type):
        '''
        Used to get the standard field type in the dynamic_model_field definition
        '''
        return STANDARD_TYPE_MAPPING.get(ogr.FieldDefn.GetTypeName(_type))