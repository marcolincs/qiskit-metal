import os
import pathlib
import math
from operator import itemgetter
from typing import TYPE_CHECKING
from typing import Dict as Dict_
from typing import List, Tuple, Union
from copy import deepcopy
import shapely
import gdspy
import geopandas
import pandas as pd

from qiskit_metal.renderers.renderer_base import QRenderer

from ... import Dict


if TYPE_CHECKING:
    # For linting typechecking, import modules that can't be loaded here under normal conditions.
    # For example, I can't import QDesign, because it requires Qrenderer first. We have the
    # chicken and egg issue.
    from qiskit_metal.designs import QDesign


class GDSRender(QRenderer):
    """Extends QRenderer to export GDS formatted files. The methods which a user will need for GDS export
    should be found within this class.

    layers:
        200 Emulated Chip size is self.scaled_max_bound = bounding_box_scale (x and y) * max_bounds of elements on chip.
        201 Holds all of the qgeometries, in a cell, which have subtract as True.
            After gdspy.boolean(), The cell is removed from lib.
            self.q_subtract_true, cell=SUBTRACT
        202 Holds all of the qgeometries which have subtract as False.
            self.q_subtract_false, cell=TOP

    datatype:
        * 10 Polygon
        * 11 Flexpath
    """

    # These options can be over-written by passing dict to render_options.
    default_options = Dict(
        # gdspy unit is 1 meter.  gds_units appear to ONLY be used during write_gds().
        # Note that gds_unit will be overwritten from the design units, during init().
        # WARNING: this cannot be changed. since it is only used during the init once.
        gds_unit=1,  # 1m

        # (float): Scale box of components to render. Should be greater than 1.0.
        bounding_box_scale_x=1.2,
        bounding_box_scale_y=1.2,

        # Implement creating a ground plane which is scaled from largest bounding box,
        # then QGeometry which is marked as subtract will be removed from ground_plane.
        # Then the balance of QGeometry will be placed placed in same layer as ground_plane.
        ground_plane=True,

        # TODO: layer numbers come from QGeometry and QDesign
        # layer and data numbers, needs to come from default options
        # i.e. self.options.ld_subtract = {"layer": 201, "data": 12}.
        #ld_subtract={"layer": 201},
        #ld_no_subtract={"layer": 202},
        ld_chip={"layer": 200, "datatype": 10},

        # DEPRECATED since using from QGeometry table now.
        # used for fillet in gdspy.FlexPath() and gdspy.boolean()
        # bend_radius_num=0.05,

        # corners ('natural', 'miter', 'bevel', 'round', 'smooth', 'circular bend', callable, list)
        # Type of joins. A callable must receive 6 arguments
        # (vertex and direction vector from both segments being joined, the center and width of the path)
        # and return a list of vertices that make the join.
        # A list can be used to define the join for each parallel path.
        corners='circular bend',

        # tolerance > precision
        # Precision used for gds lib, boolean operations and FlexPath should likely be kept the same.
        # They can be different, but increases odds of weird artifacts or misalignment.
        # Some of this occours regardless (might be related to offset of a curve when done as a boolean vs. rendered),
        # but they are <<1nm, which isn't even picked up by any fab equipment (so can be ignored)
        # Numerical errors start to pop up if set precision too fine,
        # but 1nm seems to be the finest precision we use anyhow.
        tolerance=0.00001,  # 10.0 um

        # With input from fab people, any of the weird artifacts (like unwanted gaps)
        # that are less than 1nm in size can be ignored.
        # They don't even show up in the fabricated masks.
        # So, the precision of e-9 (so 1 nm) should be good as a default.
        # TODO: Is this in meters absolute or in design units?
        precision=0.000000001   # 1.0 nm
    )

    def __init__(self, design: 'QDesign', initiate=True, render_template: Dict = None, render_options: Dict = None):
        """Create a QRenderer for GDS interface: export and import.

        Args:
            design (QDesign): Use QGeometry within QDesign  to obtain elements for GDS file.
            initiate (bool, optional): True to initiate the renderer. Defaults to True.
            render_template (Dict, optional): Typically used by GUI for template options for GDS.  Defaults to None.
            render_options (Dict, optional):  Used to overide all options. Defaults to None.
        """

        super().__init__(design=design, initiate=initiate,
                         render_template=render_template, render_options=render_options)

        self.lib = None  # type: gdspy.GdsLibrary
        self.new_gds_library()

        # depreciated
        # self.list_bounds = list()
        # key is chip_name, value is list
        self.dict_bounds = Dict()
        # self.scaled_max_bound = tuple()

        # self.all_subtract_true = geopandas.GeoDataFrame()
        # self.all_subtract_false = geopandas.GeoDataFrame()

        # Updated each time export_to_gds() is called.
        self.chip_info = dict()

        # gdspy.polygon.PolygonSet is the base class.
        self.scaled_chip_poly = gdspy.Polygon([])

        # check the scale
        self.check_bounding_box_scale()

    def check_bounding_box_scale(self):
        """Some error checking for bounding_box_scale_x and bounding_box_scale_y numbers.
        """

        # Check x
        if isinstance(self.options.bounding_box_scale_x, float) and self.options.bounding_box_scale_x >= 1.0:
            pass  # All is good.
        elif isinstance(self.options.bounding_box_scale_x, int) and self.options.bounding_box_scale_x >= 1:
            self.options.bounding_box_scale_x = float(
                self.options.bounding_box_scale_x)
        else:
            self.options['bounding_box_scale_x'] = GDSRender.default_options.bounding_box_scale_x
            self.design.logger.warning(
                f'Expected float and number greater than or equal to 1.0 for bounding_box_scale_x. \
                    User provided bounding_box_scale_x = {self.options.bounding_box_scale_x}, using default_options.bounding_box_scale_x.')

        # Check y
        if isinstance(self.options.bounding_box_scale_y, float) and self.options.bounding_box_scale_y >= 1.0:
            pass  # All is good.
        elif isinstance(self.options.bounding_box_scale_y, int) and self.options.bounding_box_scale_y >= 1:
            self.options.bounding_box_scale_y = float(
                self.options.bounding_box_scale_y)
        else:
            self.options['bounding_box_scale_y'] = GDSRender.default_options.bounding_box_scale_y
            self.design.logger.warning(
                f'Expected float and number greater than or equal to 1.0 for bounding_box_scale_y. \
                    User provided bounding_box_scale_y = {self.options.bounding_box_scale_y}, using default_options.bounding_box_scale_y.')

    def _clear_library(self):
        """Clear current library."""
        gdspy.current_library.cells.clear()

    def _can_write_to_path(self, file: str) -> int:
        """Check if can write file.

        Args:
            file (str): Has the path and/or just the file name.

        Returns:
            int: 1 if access is allowed. Else returns 0, if access not given.
        """
        directory_name = os.path.dirname(os.path.abspath(file))
        if os.access(directory_name, os.W_OK):
            return 1
        else:
            self.design.logger.warning(
                f'Not able to write to directory. File:"{file}" not written. Checked directory:"{directory_name}".')
            return 0

    def update_units(self):
        """Update the options in the units. DOES NOT CHANGE THE CURRENT LIB"""
        # Assume metal is using units smaller than 1 meter.
        self.options['gds_unit'] = 1.0 / self.design.parse_value('1 meter')

    def seperate_subtract_shapes(self, chip_name: str, table_name: str, table: geopandas.GeoSeries) -> None:
        """For each chip and table, separate them by subtract being either True or False.
           Names of chip and table should be same as the QGeometry tables.
        Args:
            chip_name (str): Name of "chip".  Example is "main".
            table_name (str): Name for "table".  Example is "poly", and "path".
            table (geopandas.GeoSeries): Table with similar qgeometries.
        """

        subtract_true = table[table['subtract'] == True]

        subtract_false = table[table['subtract'] == False]

        setattr(
            self, f'{chip_name}_{table_name}_subtract_true', subtract_true)
        setattr(
            self, f'{chip_name}_{table_name}_subtract_false', subtract_false)

    @ staticmethod
    def get_bounds(gs_table: geopandas.GeoSeries) -> Tuple[float, float, float, float]:
        """Get the bounds for all of the elements in gs_table.

        Args:
            gs_table (pandas.GeoSeries): A pandas GeoSeries used to describe components in a design.

        Returns:
            Tuple[float, float, float, float]: The bounds of all of the elements in this table. [minx, miny, maxx, maxy]
        """
        if len(gs_table) == 0:
            return(0, 0, 0, 0)

        return gs_table.total_bounds

    def inclusive_bound(self, all_bounds: list) -> tuple:
        """Given a list of tuples which describe corners of a box, i.e. (minx, miny, maxx, maxy).
        This method will find the box, which will include all boxes.  In another words, the smallest minx and miny;
        and the largest maxx and maxy.

        Args:
            all_bounds (list): List of bounds. Each tuple corresponds to a box.

        Returns:
            tuple: Describe a box which includes the area of each box in all_bounds.
        """

        # If given an empty list.
        if len(all_bounds) == 0:
            return (0.0, 0.0, 0.0, 0.0)

        inclusive_tuple = (min(all_bounds, key=itemgetter(0))[0],
                           min(all_bounds, key=itemgetter(1))[1],
                           max(all_bounds, key=itemgetter(2))[2],
                           max(all_bounds, key=itemgetter(3))[3])
        return inclusive_tuple

    # Probalby not being used and Depreciated.
    # def render_chip(self) -> None:
    #     """Use the maximum bounds for all qgeometry on chip.  Scale the size of chip.
    #        Use gdspy.Polygon() because gdspy.boolean() requires it.
    #     """

    #     # change format to use gdspy.Pologon().
    #     # [minx, miny, maxx, maxy] to [(minx,miny),(maxx,miny),(maxx,maxy),(minx,maxy)]
    #     rectangle_points = [(self.max_bound[0], self.max_bound[1]),
    #                         (self.max_bound[2], self.max_bound[1]),
    #                         (self.max_bound[2], self.max_bound[3]),
    #                         (self.max_bound[0], self.max_bound[3])]
    #     chip_poly = gdspy.Polygon(rectangle_points, **self.options.ld_chip)

    #     self.scaled_chip_poly = chip_poly.scale(
    #         scalex=self.options.bounding_box_scale_x, scaley=self.options.bounding_box_scale_y)

    def scale_max_bounds(self, chip_name: str, all_bounds: list) -> Tuple[tuple, tuple]:
        """Given the list of tuples to represent all of the bounds for path, poly, etc.
        This will return the scaled using self.bounding_box_scale_x and self.bounding_box_scale_y, and  the max bounds of the tuples provided.

        Args:
            all_bounds (list): Each tuple=(minx, miny, maxx, maxy) in list represents bounding box for poly, path, etc.

        Returns:
            tuple[tuple, tuple]:
            first tuple: A scaled bounding box which includes all paths, polys, etc.;
            second tuple: A  bounding box which includes all paths, polys, etc.
        """
        # If given an empty list.
        if len(all_bounds) == 0:
            return (0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)

        # Get an inclusive bounding box to contain all of the tuples provided.
        minx, miny, maxx, maxy = self.inclusive_bound(all_bounds)

        # Center of inclusive bounding box
        center_x = (minx + maxx) / 2
        center_y = (miny + maxy) / 2

        scaled_width = (maxx - minx) * self.options.bounding_box_scale_x
        scaled_height = (maxy - miny) * self.options.bounding_box_scale_y

        # Scaled inclusive bounding box by self.options.bounding_box_scale_x and self.options.bounding_box_scale_y.
        scaled_box = (center_x - (.5 * scaled_width),
                      center_y - (.5 * scaled_height),
                      center_x + (.5 * scaled_width),
                      center_y + (.5 * scaled_height))

        self.dict_bounds[chip_name]['scaled_box'] = scaled_box
        self.dict_bounds[chip_name]['inclusive_box'] = (minx, miny, maxx, maxy)

        return scaled_box, (minx, miny, maxx, maxy)

    # Will soon be deprecated
    # def rect_for_ground(self, chip_name: str) -> None:
    #     """Use the maximum bounds for all qgeometry on chip.  Scale the size of chip.
    #        Use gdspy.Polygon() because gdspy.boolean() requires it.

    #     Args:
    #         chip_name (str): Name of chip where the rectangle for ground plane is being generated.
    #     """

    #     rectangle_points = [(self.max_bound[0], self.max_bound[1]),
    #                         (self.max_bound[2], self.max_bound[1]),
    #                         (self.max_bound[2], self.max_bound[3]),
    #                         (self.max_bound[0], self.max_bound[3])]
    #     chip_poly = gdspy.Polygon(rectangle_points, **self.options.ld_chip)

    #     self.scaled_chip_poly = chip_poly.scale(
    #         scalex=self.options.bounding_box_scale_x, scaley=self.options.bounding_box_scale_y)

    def check_qcomps(self, highlight_qcomponents: list = []) -> Tuple[list, int]:
        """Confirm the list doesn't have names of componentes repeated.
        Comfirm that the name of component exists in QDesign.

        Args:
            highlight_qcomponents (list, optional): List of strings which denote the name of QComponents to render.
                                                     Defaults to []. Empty list means to render entire design.

        Returns:
            Tuple[list, int]:
            list: Unique list of QComponents to render.
            int: 0 if all ended well. Otherwise, 1 if QComponent name not in design.
        """
        # Remove identical QComponent names.
        unique_qcomponents = list(set(highlight_qcomponents))

        # Confirm all QComponent are in design.
        for qcomp in unique_qcomponents:
            if qcomp not in self.design.name_to_id:
                self.design.logger.warning(
                    f'The component={qcomp} in highlight_qcomponents not in QDesign. The GDS data not generated.')
                return unique_qcomponents, 1

        return unique_qcomponents, 0

    def create_qgeometry_for_gds(self, highlight_qcomponents: list = []) -> int:
        """Using self.design, this method does the following:

        1. Gather the QGeometries to be used to write to file.
           Duplicate names in hightlight_qcomponents will be removed without warning.

        2. Populate self.dict_bounds, for each chip, contains the maximum bound for all elements to render.

        3. Calculate scaled bounding box to emulate size of chip using self.bounding_box_scale(x and y)
           and place into self.scaled_max_bound.

        4. Gather Geometries to export to GDS format.

        Args:
            highlight_qcomponents (list): List of strings which denote the name of QComponents to render.
                                        If empty, render all comonents in design.
                                        If QComponent names are dupliated, duplicates will be ignored.

        Returns:
            int: 0 if all ended well. Otherwise, 1 if QComponent name not in design.
        """
        unique_qcomponents, status = self.check_qcomps(highlight_qcomponents)
        if status == 1:
            return 1
        self.dict_bounds.clear()

        for chip_name in self.chip_info:
            # put the QGeometry into GDS format.
            # There can be more than one chip in QGeometry.  They all export to one gds file.

            self.chip_info[chip_name]['all_subtract'] = []
            self.chip_info[chip_name]['all_no_subtract'] = []
            self.dict_bounds[chip_name] = Dict()
            self.dict_bounds[chip_name]['gather'] = []
            self.dict_bounds[chip_name]['for_subtract'] = tuple()
            all_table_subtracts = []
            all_table_no_subtracts = []

            for table_name in self.design.qgeometry.get_element_types():

                # Get table for chip and table_name, and reduce to keep just the list of unique_qcomponents.
                table = self.get_table(
                    table_name, unique_qcomponents, chip_name)

                # For every chip, and layer, separate the "subtract" and "no_subtract" elements and gather bounds.
                # dict_bounds[chip_name] = list_bounds
                self.gather_subtract_elements_and_bounds(
                    chip_name, table_name, table, all_table_subtracts, all_table_no_subtracts)

            # If list of QComponents provided, use the bounding_box_scale(x and y),
            # otherwise use self._chips
            scaled_max_bound, max_bound = self.scale_max_bounds(chip_name,
                                                                self.dict_bounds[chip_name]['gather'])
            if highlight_qcomponents:
                self.dict_bounds[chip_name]['for_subtract'] = scaled_max_bound
            else:
                chip_box, status = self.design.get_x_y_for_chip(chip_name)
                if status == 0:
                    self.dict_bounds[chip_name]['for_subtract'] = chip_box
                else:
                    self.dict_bounds[chip_name]['for_subtract'] = max_bound
                    self.logger.warning(
                        f'design.get_x_y_for_chip() did NOT return a good code for chip={chip_name},'
                        f'for ground subtraction-box using the size calculated from QGeometry, ({max_bound}) will be used. ')
            if self.options.ground_plane:
                self.handle_ground_plane(chip_name,
                                         all_table_subtracts, all_table_no_subtracts)

        return 0

    def handle_ground_plane(self, chip_name: str, all_table_subtracts: list, all_table_no_subtracts: list):

        minx, miny, maxx, maxy = self.dict_bounds[chip_name]['for_subtract']

        rectangle_points = [(minx, miny), (maxx, miny),
                            (maxx, maxy), (minx, maxy)]

        # While within a chip, need to to have just one rectangle for ground plane.
        self.chip_info[chip_name]['subtract_poly'] = gdspy.Polygon(
            rectangle_points, **self.options.ld_chip)

        self.chip_info[chip_name]['all_subtract_true'] = geopandas.GeoDataFrame(
            pd.concat(all_table_subtracts, ignore_index=False))
        self.chip_info[chip_name]['all_subtract_false'] = geopandas.GeoDataFrame(
            pd.concat(all_table_no_subtracts, ignore_index=False))

        self.chip_info[chip_name]['q_subtract_true'] = self.chip_info[chip_name]['all_subtract_true'].apply(
            self.qgeometry_to_gds, axis=1)

        self.chip_info[chip_name]['q_subtract_false'] = self.chip_info[chip_name]['all_subtract_false'].apply(
            self.qgeometry_to_gds, axis=1)

    def gather_subtract_elements_and_bounds(self, chip_name: str, table_name: str, table: geopandas.GeoDataFrame,
                                            all_subtracts: list, all_no_subtracts: list):

        # For every chip, and layer, separate the "subtract" and "no_subtract" elements and gather bounds.
        # f'{chip_name}_{table_name}_subtract_true'
        # f'{chip_name}_{table_name}_subtract_false'
        # dict_bounds[chip_name]

        # assume that self._chips has been populated before entering here.

        # Determine bound box and return scalar larger than size.
        bounds = tuple(self.get_bounds(table))
        # Add the bounds of each table to list.
        self.dict_bounds[chip_name]['gather'].append(bounds)

        if self.options.ground_plane:
            self.seperate_subtract_shapes(chip_name, table_name, table)

            all_subtracts.append(
                getattr(self, f'{chip_name}_{table_name}_subtract_true'))
            all_no_subtracts.append(
                getattr(self, f'{chip_name}_{table_name}_subtract_false'))

        # polys use gdspy.Polygon;    paths use gdspy.LineString
        q_geometries = table.apply(self.qgeometry_to_gds, axis=1)
        setattr(self, f'{chip_name}_{table_name}s', q_geometries)

    def get_table(self, table_name: str, unique_qcomponents: list, chip_name: str) -> geopandas.GeoDataFrame:
        """If unique_qcomponents list is empty, get table using table_name from QGeometry tables
            for all elements with table_name.  Otherwise, return a table with fewer elements, for just the
            qcomponents within the unique_qcomponents list.

        Args:
            table_name (str): Can be "path", "poly", etc. from the QGeometry tables.
            unique_qcomponents (list): User requested list of component names to export to GDS file.

        Returns:
            geopandas.GeoDataFrame: Table of elements within the QGeometry.
        """

        # self.design.qgeometry.tables is a dict. key=table_name, value=geopandas.GeoDataFrame
        if len(unique_qcomponents) == 0:
            table = self.design.qgeometry.tables[table_name]
        else:
            table = self.design.qgeometry.tables[table_name]
            # Convert string QComponent.name  to QComponent.id
            highlight_id = [self.design.name_to_id[a_qcomponent]
                            for a_qcomponent in unique_qcomponents]

            # Remove QComponents which are not requested.
            table = table[table['component'].isin(highlight_id)]

        table = table[table['chip'] == chip_name]

        return table

    def new_gds_library(self) -> gdspy.GdsLibrary:
        """Creates a new GDS Library. Deletes the old.
           Create a new GDS library file. It can contains multiple cells.

           Returns:
            gdspy.GdsLibrary: GDS library which can contain multiple celles.
        """

        self.update_units()

        if self.lib:
            self._clear_library()

        # Create a new GDS library file. It can contains multiple cells.
        self.lib = gdspy.GdsLibrary(unit=self.options.gds_unit)

        return self.lib

    def write_poly_path_to_file(self, file_name: str) -> None:
        """Using the geometries for each table name, write to a GDS file.

        -> rectangle on Y

        -> put all the 'subtract' shapes on layer X

        -> Boolean subtract X from Y and put that on Z

        -> add all the non-subtract shapes to Z as well.

        (Y = layer number 200) self.scaled_chip_poly

        (X = layer number 201) self.q_subtract_true , cell=SUBTRACT

        (Z = layer number 202) self.q_subtract_false, cell=TOP

        Args:
            file_name (str): The path and file name to write the gds file.
                             Name needs to include desired extention, i.e. "a_path_and_name.gds".
        """

        lib = self.new_gds_library()

        # The NO_EDITS cell is for testing of development code.
        # cell = lib.new_cell('NO_EDITS', overwrite_duplicate=True)

        # for table_name in self.design.qgeometry.get_element_types():
        #     q_geometries = getattr(self, f'{table_name}s')
        #     if q_geometries is None:
        #         self.design.logger.warning(
        #             f'There are no {table_name}s to write.')
        #     else:
        #         cell.add(q_geometries)

        #     if q_geometries is None:
        #         self.design.logger.warning(
        #             f'There is no table named "{table_name}s" to write.')
        #     else:
        #        cell.add(q_geometries)

        if self.options.ground_plane:
            # TODO: Note For get_chip_layer(), default is 'main'.
            chip_name = 'main'

            # # For ground plane.
            ground_cell = lib.new_cell('TOP', overwrite_duplicate=True)
            subtract_cell = lib.new_cell('SUBTRACT', overwrite_duplicate=True)
            subtract_cell.add(self.q_subtract_true)

            '''gdspy.boolean() is not documented clearly.
            If there are multiple elements to subtract (both poly and path),
            the way I could make it work is to put them into a cell, within lib.
            I used the method cell_name.get_polygons(),
            which appears to convert all elements within the cell to poly.
            After the boolean(), I deleted the cell from lib.
            The memory is freed up then.
            '''
            diff_geometry = gdspy.boolean(
                self.scaled_chip_poly,
                subtract_cell.get_polygons(),
                'not',
                precision=self.options.precision,
                layer=self.options.ld_chip.layer)

            # When getting layer from design and QGeometry.
            # layer=self.design.get_chip_layer(chip_name))

            lib.remove(subtract_cell)

            if diff_geometry is None:
                self.design.logger.warning(
                    f'There is no table named diff_geometry to write.')
            else:
                ground_cell.add(diff_geometry)

            if self.q_subtract_false is None:
                self.design.logger.warning(
                    f'There is no table named self.q_subtract_false to write.')
            else:
                ground_cell.add(self.q_subtract_false)

        lib.write_gds(file_name)

    def export_to_gds(self, file_name: str, highlight_qcomponents: list = []) -> int:
        """Use the design which was used to initialize this class.
        The QGeometry element types of both "path" and "poly", will
        be used, to convert QGeometry to GDS formatted file.

        Args:
            file_name (str): File name which can also include directory path.
                             If the file exists, it will be overwritten.
            highlight_qcomponents (list): List of strings which denote the name of QComponents to render.
                                        If empty, render all comonents in design.

        Returns:
            int: 0=file_name can not be written, otherwise 1=file_name has been written
        """

        if not self._can_write_to_path(file_name):
            return 0

        # There can be more than one chip in QGeometry.  They all export to one gds file.
        # Each chip will hold the rectangle for subtract for each layer so:
        # chip_info[chip_name][subtract_box][(min_x,min_y,max_x,max_y)]
        # chip_info[chip_name][layer_number][all_subtract_elements]
        # chip_info[chip_name][layer_number][all_no_subtract_elements]
        self.chip_info.clear()
        self.chip_info.update(self.design.qgeometry.get_chip_names())

        if (self.create_qgeometry_for_gds(highlight_qcomponents) == 0):
            self.write_poly_path_to_file(file_name)
            return 1
        else:
            return 0

    def qgeometry_to_gds(self, element: pd.Series) -> 'gdspy.polygon':
        """Convert the design.qgeometry table to format used by GDS renderer.
        Convert the class to a series of GDSII elements.

        Args:
            element (pd.Series): Expect a shapley object.

        Returns:
            'gdspy.polygon' or 'gdspy.FlexPath': Convert the class to a series of GDSII
            format on the input pd.Series.
        """

        """
        *NOTE:*
        GDS:
            points (array-like[N][2]) – Coordinates of the vertices of the polygon.
            layer (integer) – The GDSII layer number for this element.
            datatype (integer) – The GDSII datatype for this element (between 0 and 255).
                                  datatype=10 or 11 means only that they are from a
                                  Polygon vs. LineString.  This can be changed.
        See:
            https://gdspy.readthedocs.io/en/stable/reference.html#polygon
        """

        geom = element.geometry  # type: shapely.geometry.base.BaseGeometry

        if isinstance(geom, shapely.geometry.Polygon):

            # Handle  list(polygon.interiors) TODO:
            return gdspy.Polygon(list(geom.exterior.coords),
                                 layer=element.layer if not element['subtract'] else 0,
                                 # layer=element.layer,
                                 datatype=10,
                                 )
        elif isinstance(geom, shapely.geometry.LineString):
            '''
            class gdspy.FlexPath(points, width, offset=0, corners='natural', ends='flush',
            bend_radius=None, tolerance=0.01, precision=0.001, max_points=199,
            gdsii_path=False, width_transform=True, layer=0, datatype=0)

            Only fillet, if number is greater than zero.
            '''
            if math.isnan(element.fillet) or element.fillet <= 0 or element.fillet < element.width:
                to_return = gdspy.FlexPath(list(geom.coords),
                                           width=element.width,
                                           # layer=element.layer if not element['subtract'] else 0,
                                           layer=element.layer,
                                           datatype=11)
            else:
                to_return = gdspy.FlexPath(list(geom.coords),
                                           width=element.width,
                                           # layer=element.layer if not element['subtract'] else 0,
                                           layer=element.layer,
                                           datatype=11,
                                           corners=self.options.corners,
                                           bend_radius=element.fillet,
                                           tolerance=self.options.tolerance,
                                           precision=self.options.precision
                                           )
            return to_return
        else:
            # TODO: Handle
            self.design.logger.warning(
                f'Unexpected shapely object geometry.'
                f'The variable element is {type(geom)}, method can currently handle Polygon and FlexPath.')
            # print(geom)
            return None
