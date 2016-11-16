import sys
import time
import numpy as np
import scipy.optimize as opt
import scipy.ndimage.filters as filt
import scipy.signal
import matplotlib.pyplot as plt
import itertools as it
import vlbi_imaging_utils as vb
import maxen as mx
import pulses
import linearize_energy as le
from IPython import display

import math
import cmath

C = 299792458.0 
DEGREE = np.pi/180.
RADPERAS = DEGREE/3600.
RADPERUAS = RADPERAS/1e6

no_linear_shift = True #flag for whether or not to allow image shift in the phase screen

def Wrapped_Convolve(sig,ker):
    N = sig.shape[0]
    return scipy.signal.fftconvolve(np.pad(sig,((N, N), (N, N)), 'wrap'), np.pad(ker,((N, N), (N, N)), 'constant'),mode='same')[N:(2*N),N:(2*N)]

def Wrapped_Gradient(M):
    G = np.gradient(np.pad(M,((1, 1), (1, 1)), 'wrap'))
    Gx = G[0][1:-1,1:-1]
    Gy = G[1][1:-1,1:-1]
    return (Gx, Gy)
    
def MakeEpsilonScreenFromList(EpsilonList, N):
    epsilon = np.zeros((N,N),dtype=np.complex)
    #There are (N^2-1)/2 real elements followed by (N^2-1)/2 complex elements

    #The first (N-1)/2 are the top row
    N_re = (N*N-1)/2
    i = 0
    for x in range(1,(N+1)/2):
        epsilon[0][x] = EpsilonList[i] + 1j * EpsilonList[i+N_re]
        epsilon[0][N-x] = np.conjugate(epsilon[0][x])
        i=i+1

    #The next N(N-1)/2 are filling the next N rows
    for y in range(1,(N+1)/2):
        for x in range(N):
            epsilon[y][x] = EpsilonList[i] + 1j * EpsilonList[i+N_re]

            x2 = N - x
            y2 = N - y
            if x2 == N:
                x2 = 0
            if y2 == N:
                y2 = 0

            epsilon[y2][x2] = np.conjugate(epsilon[y][x])
            i=i+1    

    if no_linear_shift == True:
        epsilon[0,0] = 0
        epsilon[1,0] = 0
        epsilon[0,1] = 0
        epsilon[-1,0] = 0
        epsilon[0,-1] = 0

    return epsilon

def MakeEpsilonScreen(Nx, Ny, rngseed = 0):
    if rngseed != 0:
        np.random.seed( rngseed )

    epsilon = np.random.normal(loc=0.0, scale=1.0/math.sqrt(2), size=(Nx,Ny)) + 1j * np.random.normal(loc=0.0, scale=1.0/math.sqrt(2), size=(Nx,Ny))
    epsilon[0][0] = 0.0

    #Now let's ensure that it has the necessary conjugation symmetry
    for x in range(Nx):
        if x > (Nx-1)/2:
            epsilon[0][x] = np.conjugate(epsilon[0][Nx-x])
        for y in range((Ny-1)/2, Ny):
            x2 = Nx - x
            y2 = Ny - y
            if x2 == Nx:
                x2 = 0
            if y2 == Ny:
                y2 = 0
            epsilon[y][x] = np.conjugate(epsilon[y2][x2])

    if no_linear_shift == True:
        epsilon[0,0] = 0
        epsilon[1,0] = 0
        epsilon[0,1] = 0
        epsilon[-1,0] = 0
        epsilon[0,-1] = 0

    return epsilon

def MakePhaseScreen(EpsilonScreen,Reference_Image):
    wavelength = C/Reference_Image.rf*100.0 #Observing wavelength [cm]
    wavelengthbar = wavelength/(2.0*np.pi) #lambda/(2pi) [cm]
    D_dist = 8.023*10**21 #Observer-Scattering distance [cm]
    R_dist = 1.790*10**22 #Source-Scattering distance [cm]
    Mag = D_dist/R_dist
    r0_maj = (wavelength/0.13)**-1.0*3.134*10**8 #Phase coherence length [cm]
    r0_min = (wavelength/0.13)**-1.0*6.415*10**8 #Phase coherence length [cm]
    rF = (wavelength/0.13)**0.5*1.071*10**10 #Fresnel scale [cm]
    r_in = 1000*10**5 #inner scale [km]
    r_out = 10**20 #outer scale [km]
    scatt_alpha = 5.0/3.0 #power-law index
    FOV = Reference_Image.psize * Reference_Image.xdim * D_dist #Field of view, in cm, at the scattering screen

    def Q(qx, qy): #Power spectrum of phase fluctuations
        #x is aligned with the major axis; y is aligned with the minor axis
        qmin = 2.0*np.pi/r_out
        qmax = 2.0*np.pi/r_in
        #rotate qx and qy as needed
        PA = (90 - vb.POS_ANG) * np.pi/180.0
        qx_rot =  qx*np.cos(PA) + qy*np.sin(PA)
        qy_rot = -qx*np.sin(PA) + qy*np.cos(PA)
        return 2.0**scatt_alpha * np.pi * scatt_alpha * scipy.special.gamma(1.0 + scatt_alpha/2.0)/scipy.special.gamma(1.0 - scatt_alpha/2.0)*wavelengthbar**-2.0*(r0_maj*r0_min)**-(scatt_alpha/2.0) * ( (r0_maj/r0_min)*qx_rot**2.0 + (r0_min/r0_maj)*qy_rot**2.0 + qmin**2.0)**(-(scatt_alpha+2.0)/2.0) * np.exp(-((qx_rot**2.0 + qy_rot**2.0)/qmax**2.0)**0.5)

    Nx = EpsilonScreen.shape[1]
    Ny = EpsilonScreen.shape[0]

    #Now we'll calculate the phase screen gradient
    sqrtQ = np.zeros((Ny,Nx)) #just to get the dimensions correct
    dq = 2.0*np.pi/FOV #this is the spacing in wavenumber

    for x in range(0, Nx):
        for y in range(0, Ny):
            x2 = x
            y2 = y
            if x2 > (Nx-1)/2:
                x2 = x2 - Nx
            if y2 > (Ny-1)/2:
                y2 = y2 - Ny 
            sqrtQ[y][x] = Q(dq*x2,dq*y2)**0.5    
    sqrtQ[0][0] = 0.0 #A DC offset doesn't affect scattering

    #We'll now calculate the phase screen. We could calculate the gradient directly, but this is more bulletproof for now
    phi = np.real(wavelengthbar/FOV*EpsilonScreen.shape[0]*EpsilonScreen.shape[1]*np.fft.ifft2( sqrtQ*EpsilonScreen))
    phi_Image = vb.Image(phi, Reference_Image.psize, Reference_Image.ra, Reference_Image.dec, rf=Reference_Image.rf, source=Reference_Image.source, mjd=Reference_Image.mjd)

    return phi_Image



def reverse_array(M):
    N = M.shape[0]
    M_rot = np.copy(M)
    for x in range(N):
        for y in range(N):
            x2 = N - x
            y2 = N - y
            if x2 == N:
                x2 = 0
            if y2 == N:
                y2 = 0
            M_rot[y][x] = M[y2][x2]
    return M_rot


def Scatter(Unscattered_Image, Epsilon_Screen=np.array([]), DisplayPhi=False, DisplayImage=False): 
    #This module takes an unscattered image and Fourier components of the phase screen to produce a scattered image
    #Epsilon_Screen represents the normalized complex spectral values for the phase screen

    #Note: an odd image dimension is required

    # First some preliminary definitions
    wavelength = C/Unscattered_Image.rf*100.0 #Observing wavelength [cm]
    wavelengthbar = wavelength/(2.0*np.pi) #lambda/(2pi) [cm]
    D_dist = 8.023*10**21 #Observer-Scattering distance [cm]
    R_dist = 1.790*10**22 #Source-Scattering distance [cm]
    Mag = D_dist/R_dist
    r0_maj = (wavelength/0.13)**-1.0*3.134*10**8 #Phase coherence length [cm]
    r0_min = (wavelength/0.13)**-1.0*6.415*10**8 #Phase coherence length [cm]
    rF = (wavelength/0.13)**0.5*1.071*10**10 #Fresnel scale [cm]
    r_in = 1000*10**5 #inner scale [km]
    r_out = 10**20 #outer scale [km]
    scatt_alpha = 5.0/3.0 #power-law index
    FOV = Unscattered_Image.psize * Unscattered_Image.xdim * D_dist #Field of view, in cm, at the scattering screen

    def Q(qx, qy): #Power spectrum of phase fluctuations
        #x is aligned with the major axis; y is aligned with the minor axis
        qmin = 2.0*np.pi/r_out
        qmax = 2.0*np.pi/r_in
        #rotate qx and qy as needed
        PA = (90 - vb.POS_ANG) * np.pi/180.0
        qx_rot =  qx*np.cos(PA) + qy*np.sin(PA)
        qy_rot = -qx*np.sin(PA) + qy*np.cos(PA)
        return 2.0**scatt_alpha * np.pi * scatt_alpha * scipy.special.gamma(1.0 + scatt_alpha/2.0)/scipy.special.gamma(1.0 - scatt_alpha/2.0)*wavelengthbar**-2.0*(r0_maj*r0_min)**-(scatt_alpha/2.0) * ( (r0_maj/r0_min)*qx_rot**2.0 + (r0_min/r0_maj)*qy_rot**2.0 + qmin**2.0)**(-(scatt_alpha+2.0)/2.0) * np.exp(-((qx_rot**2.0 + qy_rot**2.0)/qmax**2.0)**0.5)


    #First we need to calculate the ensemble-average image by blurring the unscattered image with the correct kernel
    EA_Image = vb.blur_gauss(Unscattered_Image, vb.sgra_kernel_params(Unscattered_Image.rf), frac=1.0, frac_pol=0)

    if Epsilon_Screen.shape[0] == 0:
        return EA_Image
    else:
        Nx = Epsilon_Screen.shape[1]
        Ny = Epsilon_Screen.shape[0]

        #Next, we need the gradient of the ensemble-average image
        EA_Gradient = Wrapped_Gradient((EA_Image.imvec/(FOV/Nx)).reshape(EA_Image.ydim, EA_Image.xdim))    
        #The gradient signs don't actually matter, but let's make them match intuition (i.e., right to left, bottom to top)
        EA_Gradient_x = -EA_Gradient[1]
        EA_Gradient_y = -EA_Gradient[0]
    
        #Now we'll calculate the phase screen gradient
        sqrtQ = np.zeros((Ny,Nx)) #just to get the dimensions correct
        dq = 2.0*np.pi/FOV #this is the spacing in wavenumber

        for x in range(0, Nx):
            for y in range(0, Ny):
                x2 = x
                y2 = y
                if x2 > (Nx-1)/2:
                    x2 = x2 - Nx
                if y2 > (Ny-1)/2:
                    y2 = y2 - Ny 
                sqrtQ[y][x] = Q(dq*x2,dq*y2)**0.5    
        sqrtQ[0][0] = 0.0 #A DC offset doesn't affect scattering

        #We'll now calculate the phase screen. We could calculate the gradient directly, but this is more bulletproof for now
        phi = np.real(wavelengthbar/FOV*Epsilon_Screen.shape[0]*Epsilon_Screen.shape[1]*np.fft.ifft2( sqrtQ*Epsilon_Screen))
        phi_Image = vb.Image(phi, EA_Image.psize, EA_Image.ra, EA_Image.dec, rf=EA_Image.rf, source=EA_Image.source, mjd=EA_Image.mjd)
    
        if DisplayPhi:
            phi_Image.display()

        #Next, we need the gradient of the ensemble-average image
        phi_Gradient = Wrapped_Gradient(phi/(FOV/Nx))    

        #The gradient signs don't actually matter, but let's make them match intuition (i.e., right to left, bottom to top)
        phi_Gradient_x = -phi_Gradient[1]
        phi_Gradient_y = -phi_Gradient[0]

        #Now we can patch together the average image
        AI = (EA_Image.imvec).reshape(Ny,Nx) + rF**2.0 * ( EA_Gradient_x*phi_Gradient_x + EA_Gradient_y*phi_Gradient_y )

        #Optional: eliminate negative flux
        #AI = abs(AI)

        #Make it into a proper image format
        AI_Image = vb.Image(AI, EA_Image.psize, EA_Image.ra, EA_Image.dec, rf=EA_Image.rf, source=EA_Image.source, mjd=EA_Image.mjd)

        if DisplayImage:
            plot_scatt(Unscattered_Image.imvec, EA_Image.imvec, AI_Image.imvec, phi_Image.imvec, Unscattered_Image, 0, 0, ipynb=False)

        return AI_Image




##################################################################################################
# Plotting Functions
##################################################################################################
def plot_scatt_dual(im_unscatt1, im_unscatt2, im_scatt1, im_scatt2, im_phase1, im_phase2, Prior, nit, chi2, ipynb=False):
    #plot_scatt_dual(im1, im2, scatt_im1, scatt_im2, phi1, phi2, Prior1, 0, 0, ipynb=False)
    # Get vectors and ratio from current image
    x = np.array([[i for i in range(Prior.xdim)] for j in range(Prior.ydim)])
    y = np.array([[j for i in range(Prior.xdim)] for j in range(Prior.ydim)])
    
    # Create figure and title
    plt.ion()
    plt.clf()
    #plt.suptitle("step: %i  $\chi^2$: %f " % (nit, chi2), fontsize=20)
        
    # Unscattered Image
    plt.subplot(231)
    plt.imshow(im_unscatt1.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian', vmin=0)
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Unscattered')
    
    # Scattered
    plt.subplot(232)
    plt.imshow(im_scatt1.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian', vmin=0)
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Average Image')
    
    # Phase
    plt.subplot(233)
    plt.imshow(im_phase1.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian')
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Phase Screen')

      
    # Unscattered Image
    plt.subplot(234)
    plt.imshow(im_unscatt2.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian', vmin=0)
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Unscattered')
    
    # Scattered
    plt.subplot(235)
    plt.imshow(im_scatt2.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian', vmin=0)
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Average Image')
    
    # Phase
    plt.subplot(236)
    plt.imshow(im_phase2.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian')
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Phase Screen')

    # Display
    plt.draw()
    if ipynb:
        display.clear_output()
        display.display(plt.gcf())  

def plot_scatt(im_unscatt, im_ea, im_scatt, im_phase, Prior, nit, chi2, ipynb=False):
    
    # Get vectors and ratio from current image
    x = np.array([[i for i in range(Prior.xdim)] for j in range(Prior.ydim)])
    y = np.array([[j for i in range(Prior.xdim)] for j in range(Prior.ydim)])
    
    # Create figure and title
    plt.ion()
    plt.clf()
    plt.suptitle("step: %i  $\chi^2$: %f " % (nit, chi2), fontsize=20)
        
    # Unscattered Image
    plt.subplot(141)
    plt.imshow(im_unscatt.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian', vmin=0)
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Unscattered')
    

    # Ensemble Average
    plt.subplot(142)
    plt.imshow(im_ea.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian', vmin=0)
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Ensemble Average')


    # Scattered
    plt.subplot(143)
    plt.imshow(im_scatt.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian', vmin=0)
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Average Image')
    
    # Phase
    plt.subplot(144)
    plt.imshow(im_phase.reshape(Prior.ydim, Prior.xdim), cmap=plt.get_cmap('afmhot'), interpolation='gaussian')
    xticks = vb.ticks(Prior.xdim, Prior.psize/RADPERAS/1e-6)
    yticks = vb.ticks(Prior.ydim, Prior.psize/RADPERAS/1e-6)
    plt.xticks(xticks[0], xticks[1])
    plt.yticks(yticks[0], yticks[1])
    plt.xlabel('Relative RA ($\mu$as)')
    plt.ylabel('Relative Dec ($\mu$as)')
    plt.title('Phase Screen')


    # Display
    plt.draw()
    if ipynb:
        display.clear_output()
        display.display(plt.gcf())  

