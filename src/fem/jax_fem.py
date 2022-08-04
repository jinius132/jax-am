import numpy as onp
import jax
import jax.numpy as np
import os
import sys
import time
import meshio
import matplotlib.pyplot as plt
from src.fem.generate_mesh import box_mesh, cylinder_mesh, global_args

from jax.config import config
config.update("jax_enable_x64", True)

onp.random.seed(0)
onp.set_printoptions(threshold=sys.maxsize, linewidth=1000, suppress=True, precision=5)


global_args['dim'] = 3
global_args['num_quads'] = 8
global_args['num_nodes'] = 8
global_args['num_faces'] = 6


class FEM:
    def __init__(self, mesh, dirichlet_bc_info, neumann_bc_info=None):
        self.mesh = mesh
        self.dirichlet_bc_info = dirichlet_bc_info
        self.neumann_bc_info = neumann_bc_info
        self.fem_pre_computations()

    def fem_pre_computations(self):
        """Many quantities can be pre-computed and stored for better performance.
        """
        def get_shape_val_functions():
            """Hard-coded first order shape functions in the parent domain.
            Important: f1-f8 order must match "self.cells" by gmsh file!
            """
            f1 = lambda x: -1./8.*(x[0] - 1)*(x[1] - 1)*(x[2] - 1)
            f2 = lambda x: 1./8.*(x[0] + 1)*(x[1] - 1)*(x[2] - 1)
            f3 = lambda x: -1./8.*(x[0] + 1)*(x[1] + 1)*(x[2] - 1) 
            f4 = lambda x: 1./8.*(x[0] - 1)*(x[1] + 1)*(x[2] - 1)
            f5 = lambda x: 1./8.*(x[0] - 1)*(x[1] - 1)*(x[2] + 1)
            f6 = lambda x: -1./8.*(x[0] + 1)*(x[1] - 1)*(x[2] + 1)
            f7 = lambda x: 1./8.*(x[0] + 1)*(x[1] + 1)*(x[2] + 1)
            f8 = lambda x: -1./8.*(x[0] - 1)*(x[1] + 1)*(x[2] + 1)
            return [f1, f2, f3, f4, f5, f6, f7, f8]

        def get_shape_grad_functions():
            shape_fns = get_shape_val_functions()
            return [jax.grad(f) for f in shape_fns]

        def get_quad_points():
            """Pre-compute quadrature points

            Returns
            -------
            shape_vals: ndarray
                (8, 3) = (num_quads, dim)  
            """
            quad_degree = 2
            quad_points = []
            for i in range(quad_degree):
                for j in range(quad_degree):
                    for k in range(quad_degree):
                       quad_points.append([(2*(k % 2) - 1) * np.sqrt(1./3.), 
                                           (2*(j % 2) - 1) * np.sqrt(1./3.), 
                                           (2*(i % 2) - 1) * np.sqrt(1./3.)])
            quad_points = np.array(quad_points) # (quad_degree^dim, dim)
            return quad_points

        def get_face_quad_points():
            """Pre-compute face quadrature points

            Returns
            -------
            face_quad_points: ndarray
                (6, 4, 3) = (num_faces, num_face_quads, dim)  
            face_normals: ndarray
                (6, 3) = (num_faces, dim)  
            """
            face_quad_degree = 2
            face_quad_points = []
            face_normals = []
            face_extremes = np.array([-1., 1.])
            for d in range(global_args['dim']):
                for s in face_extremes:
                    s_quad_points = []
                    for i in range(face_quad_degree):
                        for j in range(face_quad_degree):
                            items = np.array([s, (2*(j % 2) - 1) * np.sqrt(1./3.), (2*(i % 2) - 1) * np.sqrt(1./3.)])
                            s_quad_points.append(list(np.roll(items, d)))            
                    face_quad_points.append(s_quad_points)
                    face_normals.append(list(np.roll(np.array([s, 0., 0.]), d)))
            face_quad_points = np.array(face_quad_points)
            face_normals = np.array(face_normals)
            return face_quad_points, face_normals

        @jax.jit
        def get_shape_vals():
            """Pre-compute shape function values

            Returns
            -------
            shape_vals: ndarray
               (8, 8) = (num_quads, num_nodes)  
            """
            shape_val_fns = get_shape_val_functions()
            quad_points = get_quad_points()
            shape_vals = []
            for quad_point in quad_points:
                physical_shape_vals = []
                for shape_val_fn in shape_val_fns:
                    physical_shape_val = shape_val_fn(quad_point) 
                    physical_shape_vals.append(physical_shape_val)
         
                shape_vals.append(physical_shape_vals)

            shape_vals = np.array(shape_vals)
            assert shape_vals.shape == (global_args['num_quads'], global_args['num_nodes'])
            return shape_vals

        # @jax.jit
        def get_shape_grads():
            """Pre-compute shape function gradients

            Returns
            -------
            shape_grads_physical: ndarray
                (cell, num_quads, num_nodes, dim)  
            JxW: ndarray
                (cell, num_quads)
            """
            shape_grad_fns = get_shape_grad_functions()
            quad_points = get_quad_points()
            shape_grads = []
            for quad_point in quad_points:
                physical_shape_grads = []
                for shape_grad_fn in shape_grad_fns:
                    # See Hughes, Thomas JR. The finite element method: linear static and dynamic finite element analysis. Courier Corporation, 2012.
                    # Page 147, Eq. (3.9.3)
                    physical_shape_grad = shape_grad_fn(quad_point)
                    physical_shape_grads.append(physical_shape_grad)
         
                shape_grads.append(physical_shape_grads)

            shape_grads = np.array(shape_grads) # (num_quads, num_nodes, dim)
            assert shape_grads.shape == (global_args['num_quads'], global_args['num_nodes'], global_args['dim'])

            physical_coos = np.take(self.points, self.cells, axis=0) # (num_cells, num_nodes, dim)

            # (num_cells, num_quads, num_nodes, dim, dim) -> (num_cells, num_quads, 1, dim, dim)
            jacobian_dx_deta = np.sum(physical_coos[:, None, :, :, None] * shape_grads[None, :, :, None, :], axis=2, keepdims=True)
            jacobian_det = np.linalg.det(jacobian_dx_deta)[:, :, 0] # (num_cells, num_quads)
            jacobian_deta_dx = np.linalg.inv(jacobian_dx_deta)
            shape_grads_physical = (shape_grads[None, :, :, None, :] @ jacobian_deta_dx)[:, :, :, 0, :]

            # For first order FEM with 8 quad points, those quad weights are all equal to one
            quad_weights = 1.
            JxW = jacobian_det * quad_weights
            return shape_grads_physical, JxW

        # @jax.jit
        def get_face_shape_vals():
            """Pre-compute face shape function values

            Returns
            -------
            face_shape_vals: ndarray
               (6, 4, 8) = (num_faces, num_face_quads, num_nodes)  
            """
            shape_val_fns = get_shape_val_functions()
            face_quad_points, _ = get_face_quad_points()
            face_shape_vals = []
            for f_quad_points in face_quad_points:
                f_shape_vals = []
                for quad_point in f_quad_points:
                    physical_shape_vals = []
                    for shape_val_fn in shape_val_fns:
                        physical_shape_val = shape_val_fn(quad_point) 
                        physical_shape_vals.append(physical_shape_val)
                    f_shape_vals.append(physical_shape_vals)
                face_shape_vals.append(f_shape_vals)
            face_shape_vals = np.array(face_shape_vals)
            return face_shape_vals

        @jax.jit
        def get_face_Nanson_scale():
            """Pre-compute face JxW
            Reference: https://en.wikiversity.org/wiki/Continuum_mechanics/Volume_change_and_area_change

            Returns
            ------- 
            JxW: ndarray
                (num_cells, num_faces, num_face_quads)
            """
            shape_grad_fns = get_shape_grad_functions()
            face_quad_points, face_normals = get_face_quad_points() # _, (num_faces, dim)

            face_shape_grads = []
            for f_quad_points in face_quad_points:
                f_shape_grads = []
                for f_quad_point in f_quad_points:
                    physical_shape_grads = []
                    for shape_grad_fn in shape_grad_fns:
                        physical_shape_grad = shape_grad_fn(f_quad_point)
                        physical_shape_grads.append(physical_shape_grad)
                    f_shape_grads.append(physical_shape_grads)
                face_shape_grads.append(f_shape_grads)

            face_shape_grads = np.array(face_shape_grads) # (num_faces, num_face_quads, num_nodes, dim)

            physical_coos = np.take(self.points, self.cells, axis=0) # (num_cells, num_nodes, dim)

            # (num_cells, num_faces, num_face_quads, num_nodes, dim, dim) -> (num_cells, num_faces, num_face_quads, dim, dim)
            jacobian_dx_deta = np.sum(physical_coos[:, None, None, :, :, None] * face_shape_grads[None, :, :, :, None, :], axis=3)
            jacobian_det = np.linalg.det(jacobian_dx_deta) # (num_cells, num_faces, num_face_quads)
            jacobian_deta_dx = np.linalg.inv(jacobian_dx_deta) # (num_cells, num_faces, num_face_quads, dim, dim)

            # (num_cells, num_faces, num_face_quads)
            nanson_scale = np.linalg.norm((face_normals[None, :, None, None, :] @ jacobian_deta_dx)[:, :, :, 0, :], axis=-1)
            quad_weights = 1.
            nanson_scale = nanson_scale * jacobian_det * quad_weights

            return nanson_scale

        def get_face_inds():
            """Hard-coded reference node points.
            Important: order must match "self.cells" by gmsh file!
            """
            # TODO
            node_points = np.array([[-1., -1., -1.],
                                    [1., -1, -1.],
                                    [1., 1., -1.],
                                    [-1., 1., -1.],
                                    [-1., -1., 1.],
                                    [1., -1, 1.],
                                    [1., 1., 1.],
                                    [-1., 1., 1.]])
            face_inds = []
            face_extremes = np.array([-1., 1.])
            for d in range(global_args['dim']):
                for s in face_extremes:
                    face_inds.append(np.argwhere(np.isclose(node_points[:, d], s)).reshape(-1))
            face_inds = np.array(face_inds)      

            return face_inds

        self.points = self.mesh.points
        self.cells = self.mesh.cells

        global_args['num_cells'] = len(self.cells)
        global_args['num_total_vertices'] = len(self.mesh.points)
        self.shape_vals = get_shape_vals()
        self.shape_grads, self.JxW = get_shape_grads()

        # TODO: Do delete
        if self.neumann_bc_info is not None:
            self.face_shape_vals = get_face_shape_vals()
            self.face_scale =  get_face_Nanson_scale()
            self.face_inds = get_face_inds()


    def get_physical_quad_points(self):
        pass

    def get_physical_surface_quad_points(self, cells_surface):
        pass   


    def Dirichlet_boundary_conditions(self):
        """
        """
        location_fns, vecs, value_fns = self.dirichlet_bc_info
        # TODO: add assertion for vecs, vecs must only contain 0 or 1 or 2, and must be integer
        assert len(location_fns) == len(value_fns) and len(value_fns) == len(vecs)
        inds_node_list = []
        vec_inds_list = []
        vals_list = []
        for i in range(len(location_fns)):
            node_inds = np.argwhere(jax.vmap(location_fns[i])(self.mesh.points)).reshape(-1)
            vec_inds = np.ones_like(node_inds, dtype=np.int32)*vecs[i]
            values = jax.vmap(value_fns[i])(self.mesh.points[node_inds])
            inds_node_list.append(node_inds)
            vec_inds_list.append(vec_inds)
            vals_list.append(values)
        return inds_node_list, vec_inds_list, vals_list


    def Neuman_boundary_conditions(self):
        """
        """
        location_fns = self.neumann_bc_info
        cell_points = np.take(self.points, self.cells, axis=0)
        cell_face_points = np.take(cell_points, self.face_inds, axis=1) # (num_cells, num_faces, num_face_nodes, dim)
        boundary_inds_list = []
        for i in range(len(location_fns)):
            vmap_location_fn = jax.vmap(location_fns[i])
            def on_boundary(cell_points):
                boundary_flag = vmap_location_fn(cell_points)
                return np.all(boundary_flag)
            vvmap_on_boundary = jax.vmap(jax.vmap(on_boundary))
            boundary_flags = vvmap_on_boundary(cell_face_points)
            boundary_inds = np.argwhere(boundary_flags) # (num_selected_faces, 2)
            boundary_inds_list.append(boundary_inds)
        return boundary_inds_list  


class Laplace(FEM):
    def __init__(self, mesh, dirichlet_bc_info, neumann_bc_info=None):
        super().__init__(mesh, dirichlet_bc_info, neumann_bc_info)    
        self.rhs = self.get_rhs()
        self.neumann = self.compute_Neumann_integral()

    def compute_residual(self, dofs):
        """

        Parameters
        ----------
        dofs: ndarray
            (num_nodes, vec) 
        """
        # (num_cells, 1, num_nodes, vec, 1) * (num_cells, num_quads, num_nodes, 1, dim) -> (num_cells, num_quads, num_nodes, vec, dim) 
        u_grads = np.take(dofs, self.cells, axis=0)[:, None, :, :, None] * self.shape_grads[:, :, :, None, :] 
        u_grads = np.sum(u_grads, axis=2, keepdims=True) # (num_cells, num_quads, 1, vec, dim)  
        u_physics = self.compute_physics(dofs, u_grads) # (num_cells, num_quads, 1, vec, dim)  
        v_grads = self.shape_grads[:, :, :, None, :] # (num_cells, num_quads, num_nodes, vec, dim)
        # (num_cells, num_nodes, vec) -> (num_cells*num_nodes, vec)
        weak_form = np.sum(u_physics * v_grads * self.JxW[:, :, None, None, None], axis=(1, -1)).reshape(-1, self.vec) 
        res = np.zeros_like(dofs)
        res = res.at[self.cells.reshape(-1)].add(weak_form)
        return res - self.rhs - self.neumann

    def compute_physics(self, dofs, u_grads):
        """Default
        """
        return u_grads

    def get_rhs(self):
        """Default
        """
        rhs = np.zeros((global_args['num_total_vertices'], self.vec))
        return rhs

    def get_traction(self):
        """TODO
        """
        pass

    def compute_Neumann_integral(self):
        integral = np.zeros((global_args['num_total_vertices'], self.vec))

        if self.neumann_bc_info is not None:
            integral = np.zeros((global_args['num_total_vertices'], self.vec))
            boundary_inds_list = self.Neuman_boundary_conditions()

            for i, boundary_inds in enumerate(boundary_inds_list):

                # TODO
                traction = np.array([1., 0., 0.])
                
                # (num_cells, num_faces, num_face_quads) -> (num_selected_faces, num_face_quads)
                scale = self.face_scale[boundary_inds[:, 0], boundary_inds[:, 1]]
                # (num_faces, num_face_quads, num_nodes) ->  (num_selected_faces, num_face_quads, num_nodes)
                v_vals = np.take(self.face_shape_vals, boundary_inds[:, 1], axis=0)
                v_vals = np.repeat(v_vals[:, :, :, None], self.vec, axis=-1) # (num_selected_faces, num_face_quads, num_nodes, vec)
                subset_cells = np.take(self.cells, boundary_inds[:, 0], axis=0) # (num_selected_faces, num_nodes)

                # (num_selected_faces, num_nodes, vec) -> (num_selected_faces*num_nodes, vec)
                int_vals = np.sum(v_vals * traction[None, None, None, :] * scale[:, :, None, None], axis=1).reshape(-1, self.vec) 
                integral = integral.at[subset_cells.reshape(-1)].add(int_vals)   

        return integral

    def save_sol(self, sol):
        out_mesh = meshio.Mesh(points=self.points, cells={'hexahedron': self.cells})
        out_mesh.point_data['sol'] = onp.array(sol.reshape((global_args['num_total_vertices'], self.vec)), dtype=onp.float32)
        out_mesh.write(f"post-processing/vtk/fem/jax_{self.name}.vtu")


class LinearPoisson(Laplace):
    def __init__(self, mesh, dirichlet_bc_info, neumann_bc_info=None):
        super().__init__(mesh, dirichlet_bc_info, neumann_bc_info)
        self.body_force = self.get_body_force()
        self.name = 'linear_poisson'
        self.vec = 1

    def get_body_force(self):
        """Pre-compute right-hand-side body force
        In a more general case, this should return ndarray (num_cells, num_quads) ?
        """
        return 10.

    def get_rhs(self):
        rhs = np.zeros((global_args['num_total_vertices'], self.vec))
        v_vals = np.repeat(self.shape_vals[None, :, :, None], global_args['num_cells'], axis=0) # (num_cells, num_quads, num_nodes, 1)
        v_vals = np.repeat(v_vals, self.vec, axis=-1) # (num_cells, num_quads, num_nodes, vec)
        rhs_vals = np.sum(v_vals * self.body_force * self.JxW[:, :, None, None], axis=1).reshape(-1, self.vec) # (num_cells, num_nodes, vec) -> (num_cells*num_nodes, vec)
        rhs = rhs.at[self.cells.reshape(-1)].add(rhs_vals)   
        return rhs

    # def get_boundary_idx(self):
    #     EPS = 1e-5
    #     left_inds_x_nodes = onp.argwhere(self.mesh.points[:, 0] < EPS).reshape(-1)
    #     left_inds_x_vec = onp.zeros_like(left_inds_x_nodes)
    #     right_inds_x_nodes = onp.argwhere(self.mesh.points[:, 0] >  global_args['domain_x'] - EPS).reshape(-1)
    #     right_inds_x_vec = onp.zeros_like(right_inds_x_nodes)
    #     node_inds_list = [left_inds_x_nodes, right_inds_x_nodes]
    #     vec_inds_list = [left_inds_x_vec, right_inds_x_vec]
    #     vals_list = [0., 0.]
    #     return node_inds_list, vec_inds_list, vals_list


class NonelinearPoisson(Laplace):
    def __init__(self, mesh, dirichlet_bc_info, neumann_bc_info=None):
        super().__init__(mesh, dirichlet_bc_info, neumann_bc_info)
        self.name = 'nonlinear_poisson'

    def compute_physics(self, dofs, u_grads):
        """

        Parameters
        ----------
        u_grads: ndarray
            (num_cells, num_quads, 1, vec, dim)
        """
        # (num_cells, 1, num_nodes, vec) * (1, num_quads, num_nodes, 1) -> (num_cells, num_quads, num_nodes, vec)
        u_vals = np.take(dofs, self.cells, axis=0)[:, None, :, :] * self.shape_vals[None, :, :, None] 
        u_vals = np.sum(u_vals, axis=2, keepdims=True) # (num_cells, num_quads, 1, vec)
        q = (1 + u_vals**2)[:, :, :, :, None] # (num_cells, num_quads, 1, vec, 1)
        return q * u_grads


class LinearElasticity(Laplace):
    def __init__(self, name, mesh, dirichlet_bc_info, neumann_bc_info=None):
        # TODO
        self.vec = 3
        self.name = name
        super().__init__(mesh, dirichlet_bc_info, neumann_bc_info)

    def compute_physics(self, dofs, u_grads):
        u_grads_reshape = u_grads.reshape(-1, self.vec, global_args['dim'])

        def strain(u_grad):
            E = 100.
            nu = 0.3
            mu = E/(2.*(1. + nu))
            lmbda = E*nu/((1+nu)*(1-2*nu))
            eps = 0.5*(u_grad + u_grad.T)
            sigma = lmbda*np.trace(eps)*np.eye(global_args['dim']) + 2*mu*eps
            return sigma

        strain_vmap = jax.vmap(strain)
        stress = strain_vmap(u_grads_reshape).reshape(u_grads.shape)
        return stress

    # def get_boundary_idx_free_tensile(self):
    #     """Alternative boundary conditions
    #     """
    #     EPS = 1e-5
    #     left_inds_x_nodes = onp.argwhere(self.mesh.points[:, 0] < EPS).reshape(-1)
    #     left_inds_x_vec = onp.zeros_like(left_inds_x_nodes)
    #     right_inds_x_nodes = onp.argwhere(self.mesh.points[:, 0] >  global_args['domain_x'] - EPS).reshape(-1)
    #     right_inds_x_vec = onp.zeros_like(right_inds_x_nodes)
    #     corner_inds_yz_nodes = onp.array([0, 0])
    #     corner_inds_yz_vec = onp.array([1, 2])
    #     node_inds_list = [left_inds_x_nodes, right_inds_x_nodes, corner_inds_yz_nodes]
    #     vec_inds_list = [left_inds_x_vec, right_inds_x_vec, corner_inds_yz_vec]
    #     vals_list = [0., 0.1, 0.]
    #     return node_inds_list, vec_inds_list, vals_list
  

    # def get_boundary_idx_1(self):
    #     # TODO
    #     EPS = 1e-5
    #     node_inds_list = []
    #     vec_inds_list = []
    #     inds = [0, 1, 2]
    #     for i in range(len(inds)):
    #         left_inds_nodes = onp.argwhere(self.mesh.points[:, 0] < EPS).reshape(-1)
    #         left_inds_vec = onp.ones_like(left_inds_nodes, dtype=np.int32)*inds[i]
    #         right_inds_nodes = onp.argwhere(self.mesh.points[:, 0] > global_args['domain_x'] - EPS).reshape(-1)
    #         right_inds_vec = onp.ones_like(right_inds_nodes, dtype=np.int32)*inds[i]
    #         node_inds_list += [left_inds_nodes, right_inds_nodes]
    #         vec_inds_list += [left_inds_vec, right_inds_vec]

    #     vals_list = [0., 0.1, 0., 0., 0., 0.] # left_x, right_x, left_y, right_y, left_z, right_z
    #     return node_inds_list, vec_inds_list, vals_list


    # def get_boundary_idx(self):
    #     # TODO
    #     EPS = 1e-5
 
    #     node_inds_list = []
    #     vec_inds_list = []
    #     inds = [0, 1, 2]
    #     H = 10.
    #     for i in range(len(inds)):
    #         bottom_inds_nodes = onp.argwhere(self.mesh.points[:, 2] < EPS).reshape(-1)
    #         bottom_inds_vec = onp.ones_like(bottom_inds_nodes, dtype=np.int32)*inds[i]
    #         node_inds_list += [bottom_inds_nodes]
    #         vec_inds_list += [bottom_inds_vec]

    #     vals_list = [0., 0., 0] # bottom_x, bottom_y, bottom_z
    #     return node_inds_list, vec_inds_list, vals_list


def solver(problem):
    def operator_to_matrix(operator_fn):
        J = jax.jacfwd(operator_fn)(np.zeros(global_args['num_total_vertices']*problem.vec))
        return J

    def apply_bc(res_fn, node_inds_list, vec_inds_list, vals_list):
        def A_fn(dofs):
            """Apply Dirichlet boundary conditions
            """
            dofs = dofs.reshape(global_args['num_total_vertices'], problem.vec)
            res = res_fn(dofs)
            for i in range(len(node_inds_list)):
                res = res.at[node_inds_list[i], vec_inds_list[i]].set(dofs[node_inds_list[i], vec_inds_list[i]], unique_indices=True)
                res = res.at[node_inds_list[i], vec_inds_list[i]].add(-vals_list[i])
            return res.reshape(-1)
        
        return A_fn

    def get_A_fn_linear_fn(sol):
        def A_fn_linear_fn(inc):
            primals, tangents = jax.jvp(A_fn, (sol,), (inc,))
            return tangents
        return A_fn_linear_fn

    def get_A_fn_linear_fn_JFNK(sol):
        def A_fn_linear_fn(inc):
            EPS = 1e-3
            return (A_fn(sol + EPS*inc) - A_fn(sol))/EPS
        return A_fn_linear_fn

    res_fn = problem.compute_residual

    node_inds_list, vec_inds_list, vals_list = problem.Dirichlet_boundary_conditions()

    print("Done pre-computing")

    A_fn = apply_bc(res_fn, node_inds_list, vec_inds_list, vals_list)

    start = time.time()

    sol = np.zeros((global_args['num_total_vertices'], problem.vec))
    for i in range(len(node_inds_list)):
        sol = sol.at[node_inds_list[i], vec_inds_list[i]].set(vals_list[i])
    sol = sol.reshape(-1)

    tol = 1e-6  
    step = 0
    b = -A_fn(sol)
    res_val = np.linalg.norm(b)
    print(f"step = {step}, res l_2 = {res_val}") 
    while res_val > tol:
        A_fn_linear = get_A_fn_linear_fn(sol)
        debug = False
        if debug:
            # Check onditional number of the matrix
            A_dense = operator_to_matrix(A_fn_linear)
            print(np.linalg.cond(A_dense))
            print(np.max(A_dense))
            # print(A_dense)

        inc, info = jax.scipy.sparse.linalg.bicgstab(A_fn_linear, b, x0=None, M=None, tol=1e-10, atol=1e-10, maxiter=10000) # bicgstab
        sol = sol + inc
        b = -A_fn(sol)
        res_val = np.linalg.norm(b)
        step += 1
        print(f"step = {step}, res l_2 = {res_val}") 

    end = time.time()
    solve_time = end - start
    print(f"Solve took {solve_time} [s], finished in {step} steps")
    print(f"max of sol = {np.max(sol)}")
    print(f"min of sol = {np.min(sol)}")

    problem.save_sol(sol)

    return solve_time


def test_surface_integral():
    mesh = cylinder_mesh()

    problem = FEM(mesh)

    print(problem.face_inds)

    # print(np.argwhere(problem.points[]))
 
    H = 10.
    R = 5.
    def top_boundary(x):
        H = 10.
        return np.isclose(x[2], H, atol=1e-5)


    boundary_inds = problem.get_Neumman_boundary_inds(top_boundary)

    area = np.sum(problem.face_scale[boundary_inds[:, 0], boundary_inds[:, 1]])


    print(f"True area is {np.pi*R**2}")
    print(f"FEM area is {area}")


class Mesh():
    def __init__(self, points, cells):
        self.points = points
        self.cells = cells


def debug():
 
    mesh = cylinder_mesh()
    cells = mesh.cells_dict['hexahedron'] 
    points = mesh.points

    # 0, 14 useless
    points = np.vstack((points[1:14], points[15:]))
    cells = onp.where(cells > 14, cells - 2, cells - 1)

    mesh = Mesh(points, cells)


    H = 10.
    R = 5.

    def top(point):
        return np.isclose(point[2], H, atol=1e-5)

    def bottom(point):
        return np.isclose(point[2], 0., atol=1e-5)

    def zero_val(point):
        return 0.

    def nonzero_val(point):
        return 1.

    location_fns = [bottom, bottom, bottom]
    value_fns = [zero_val, zero_val, zero_val]
    vecs = [0, 1, 2]
    dirichlet_bc_info = [location_fns, vecs, value_fns]

    # location_fns = [bottom, bottom, bottom, top, top, top]
    # value_fns = [zero_val, zero_val, zero_val, zero_val, zero_val, nonzero_val]
    # vecs = [0, 1, 2, 0, 1, 2]
    # dirichlet_bc_info = [location_fns, vecs, value_fns]

    neumann_bc_info = [top]


    problem = LinearElasticity('linear_elasticity_cylinder', mesh, dirichlet_bc_info, neumann_bc_info)
    st = solver(problem)


    # mesh = box_mesh(10, 10, 10)
    # problem = LinearElasticity(mesh)
    # st = solver(problem)


def performance_test():
    # Problems = [LinearElasticity, LinearPoisson, NonelinearPoisson]
    Problems = [LinearElasticity]

    # Ns = [25, 50, 100]
    Ns = [10]

    solve_time = []
    for Problem in Problems:
        prob_time = []
        for N in Ns:
            mesh = box_mesh(N, N, N)
            problem = Problem(mesh)
            st = solver(problem)
            prob_time.append(st)
        solve_time.append(prob_time)
    
    solve_time = onp.array(solve_time)
    platform = jax.lib.xla_bridge.get_backend().platform
    onp.savetxt(f"post-processing/txt/jax_fem_{platform}_time.txt", solve_time, fmt='%.3f')
    print(solve_time)


if __name__ == "__main__":
    # box_mesh()
    # performance_test()
    # test_surface_integral()
    debug()
