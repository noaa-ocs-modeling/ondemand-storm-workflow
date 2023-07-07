# Parameters
storm=$1
year=$2
subset_mesh=1
# Other params
hr_prelandfall=-1
#past_forecast_flag = "--past-forecast" if ...
num_perturb=50
sample_rule='korobov'
spinup_exec='pschism_PAHM_TVD-VL'
hotstart_exec='pschism_PAHM_TVD-VL'

# Paths as local variables
L_NWM_DATASET=/lustre/nwm/NWM_v2.0_channel_hydrofabric/nwm_v2_0_hydrofabric.gdb
L_TPXO_DATASET=/lustre/tpxo
L_DEM_HI=/lustre/dem/ncei19/*.tif
L_DEM_LO=/lustre/dem/gebco/*.tif
L_MESH_HI=/lustre/grid/HSOFS_250m_v1.0_fixed.14
L_MESH_LO=/lustre/grid/WNAT_1km.14
L_SHP_DIR=/lustre/shape
L_IMG_DIR=`realpath ./scripts`
L_SCRIPT_DIR=`realpath ./scripts`

# Environment
export SINGULARITY_BINDFLAGS="--bind /lustre"
export TMPDIR=/lustre/.tmp  # redirect OCSMESH temp files

# Modules
L_SOLVE_MODULES="openmpi/4.1.2"
